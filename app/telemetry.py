"""OpenTelemetry: traces, metrics and logs over OTLP (e.g. to Grafana Alloy). Off unless configured.

Configured with the standard environment variables, which the OTLP exporters also read themselves:

    OTEL_EXPORTER_OTLP_ENDPOINT   http://alloy:4318 (http/protobuf) or http://alloy:4317 (grpc). Unset = off.
    OTEL_EXPORTER_OTLP_PROTOCOL   http/protobuf (default) | grpc
    OTEL_EXPORTER_OTLP_HEADERS    e.g. "authorization=Basic …"
    OTEL_SERVICE_NAME             default "claude-headless-api"
    OTEL_RESOURCE_ATTRIBUTES      e.g. "deployment.environment=prod"
    OTEL_TRACES_EXPORTER / OTEL_METRICS_EXPORTER / OTEL_LOGS_EXPORTER   otlp (default) | none
    OTEL_METRIC_EXPORT_INTERVAL   milliseconds, default 60000
    OTEL_SDK_DISABLED             true = off even with an endpoint

Privacy: prompts and replies never become span attributes, metric labels or log fields. SQL spans carry
statements without bound parameters, and message content only ever travels as a parameter.
"""

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app import __version__

log = logging.getLogger("uvicorn.error")

# Structured per-request log records (exported as OTel logs; quiet on the console).
request_logger = logging.getLogger("claude_api.requests")

DEFAULT_SERVICE_NAME = "claude-headless-api"
# Docker health checks and static assets would drown the useful spans.
EXCLUDED_URLS = "healthz,static/,favicon.ico"


@dataclass
class _Instruments:
    runs: Any
    run_duration: Any
    queue_wait: Any
    tokens: Any
    cost: Any
    requests: Any


@dataclass
class _State:
    enabled: bool = False
    tracer: Any = trace.NoOpTracer()
    instruments: _Instruments | None = None
    providers: tuple = ()
    handlers: tuple = ()
    app: Any = None


_state = _State()


def _build_instruments(meter) -> _Instruments:
    return _Instruments(
        runs=meter.create_counter("claude.runs", unit="{run}", description="Claude CLI runs by outcome"),
        run_duration=meter.create_histogram(
            "claude.run.duration", unit="s", description="Time the Claude CLI process ran (excludes queueing)"
        ),
        queue_wait=meter.create_histogram(
            "claude.queue.wait", unit="s", description="Time spent waiting for a free Claude slot"
        ),
        tokens=meter.create_counter("claude.tokens", unit="{token}", description="Tokens reported by Claude Code"),
        cost=meter.create_counter("claude.cost", unit="{USD}", description="Cost reported by Claude Code"),
        requests=meter.create_counter(
            "api.requests", unit="{request}", description="API and dashboard-chat requests by outcome"
        ),
    )


_NOOP = _build_instruments(metrics.NoOpMeterProvider().get_meter("noop"))


def enabled() -> bool:
    return _state.enabled


class _DropNoisySpans:
    """Wraps a span processor and drops spans that only add noise to every trace:

    - SQLAlchemy's `connect` span, emitted on every connection checkout;
    - SQL spans with no parent request (startup schema checks, the retention loop), which would
      otherwise each become a one-span trace.
    """

    def __init__(self, inner):
        self._inner = inner

    def on_start(self, span, parent_context=None):
        self._inner.on_start(span, parent_context=parent_context)

    def on_end(self, span):
        scope = span.instrumentation_scope.name if span.instrumentation_scope else ""
        is_sql = scope.startswith("opentelemetry.instrumentation.sqlalchemy")
        if is_sql and (span.name == "connect" or span.parent is None):
            return
        self._inner.on_end(span)

    def _on_ending(self, span):  # newer SDKs call this on processors that define it
        hook = getattr(self._inner, "_on_ending", None)
        if hook:
            hook(span)

    def shutdown(self):
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _exporter_wanted(signal: str) -> bool:
    return os.environ.get(f"OTEL_{signal}_EXPORTER", "otlp").strip().lower() != "none"


def _otlp_exporters():
    """Build OTLP exporters for the configured protocol; endpoint and headers come from the env."""
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").strip().lower()
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    elif protocol in ("http/protobuf", "http"):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        raise RuntimeError(f"Unsupported OTEL_EXPORTER_OTLP_PROTOCOL '{protocol}' (use http/protobuf or grpc).")
    return (
        OTLPSpanExporter() if _exporter_wanted("TRACES") else None,
        OTLPMetricExporter() if _exporter_wanted("METRICS") else None,
        OTLPLogExporter() if _exporter_wanted("LOGS") else None,
    )


def setup(app, engine, *, span_exporter=None, metric_reader=None, log_exporter=None) -> bool:
    """Wire OpenTelemetry into `app` and `engine`. Without explicit exporters (tests pass in-memory
    ones), it is configured from the environment and does nothing unless an OTLP endpoint is set."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    testing = any(x is not None for x in (span_exporter, metric_reader, log_exporter))
    if not testing:
        if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
            return False
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return False
        span_exporter, metric_exporter, log_exporter = _otlp_exporters()
        metric_reader = PeriodicExportingMetricReader(metric_exporter) if metric_exporter else None

    attributes = {"service.version": __version__}
    if not os.environ.get("OTEL_SERVICE_NAME"):  # attributes passed here would override the env var
        attributes["service.name"] = DEFAULT_SERVICE_NAME
    resource = Resource.create(attributes)

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(_DropNoisySpans(BatchSpanProcessor(span_exporter)))
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader] if metric_reader else [])
    logger_provider = LoggerProvider(resource=resource)
    handlers: tuple = ()
    if log_exporter is not None:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        # Our loggers plus uvicorn's (startup, errors); uvicorn's access log is left out, as HTTP spans cover it.
        for name in ("claude_api", "uvicorn.error"):
            logging.getLogger(name).addHandler(handler)
        logging.getLogger("claude_api").setLevel(logging.INFO)
        handlers = (handler,)

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        excluded_urls=EXCLUDED_URLS,
        exclude_spans=["receive", "send"],  # one internal span per ASGI message is pure noise
    )
    SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=tracer_provider, meter_provider=meter_provider)

    meter = meter_provider.get_meter("claude_headless_api", __version__)
    instruments = _build_instruments(meter)
    _register_gate_gauges(meter)

    _state.enabled = True
    _state.tracer = tracer_provider.get_tracer("claude_headless_api", __version__)
    _state.instruments = instruments
    _state.providers = (tracer_provider, meter_provider, logger_provider)
    _state.handlers = handlers
    _state.app = app
    if not testing:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        log.info("OpenTelemetry export enabled → %s", endpoint)
    return True


def _register_gate_gauges(meter) -> None:
    from opentelemetry.metrics import Observation

    from app.claude_runner import gate

    meter.create_observable_gauge(
        "claude.in_flight",
        callbacks=[lambda _options: [Observation(gate.in_flight)]],
        unit="{run}",
        description="Claude CLI processes running now",
    )
    meter.create_observable_gauge(
        "claude.queued",
        callbacks=[lambda _options: [Observation(gate.queued)]],
        unit="{request}",
        description="Requests waiting for a free Claude slot",
    )
    meter.create_observable_gauge(
        "claude.concurrency_limit",
        callbacks=[lambda _options: [Observation(gate.limit)]],
        unit="{run}",
        description="Configured maximum of concurrent Claude runs",
    )


def shutdown() -> None:
    """Flush and stop exporters (app shutdown), and undo instrumentation so setup() can run again."""
    if not _state.enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    for handler in _state.handlers:
        for name in ("claude_api", "uvicorn.error"):
            logging.getLogger(name).removeHandler(handler)
    for provider in _state.providers:
        provider.shutdown()
    if _state.app is not None:
        FastAPIInstrumentor.uninstrument_app(_state.app)
    SQLAlchemyInstrumentor().uninstrument()
    _state.__init__()


def force_flush() -> None:
    for provider in _state.providers:
        provider.force_flush()


# --- helpers used by the rest of the app --------------------------------------------------


def tracer():
    return _state.tracer


def instruments() -> _Instruments:
    return _state.instruments or _NOOP


@contextmanager
def claude_span(model: str, effort: str, mode: str):
    """The span around one Claude run, following the GenAI semantic conventions (no content)."""
    with _state.tracer.start_as_current_span(
        f"chat {model}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": model,
            "claude.effort": effort,
            "claude.session_mode": mode,
        },
    ) as span:
        yield span


def record_run(span, *, model: str, effort: str, mode: str, outcome: str, queue_wait_s: float,
               run_s: float | None, usage: dict | None, cost_usd: float | None, error: str | None = None) -> None:  # fmt: skip
    attrs = {"gen_ai.request.model": model, "claude.effort": effort, "claude.session_mode": mode, "outcome": outcome}
    ins = instruments()
    ins.runs.add(1, attrs)
    ins.queue_wait.record(queue_wait_s, {"gen_ai.request.model": model})
    if run_s is not None:
        ins.run_duration.record(run_s, attrs)
    span.set_attribute("claude.outcome", outcome)
    span.set_attribute("claude.queue_wait_ms", int(queue_wait_s * 1000))
    if usage:
        token_types = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "cache_creation": usage.get("cache_creation_input_tokens"),
        }
        for token_type, n in token_types.items():
            if n:
                ins.tokens.add(n, {"gen_ai.request.model": model, "gen_ai.token.type": token_type})
        span.set_attribute("gen_ai.usage.input_tokens", usage.get("input_tokens") or 0)
        span.set_attribute("gen_ai.usage.output_tokens", usage.get("output_tokens") or 0)
    if cost_usd:
        ins.cost.add(cost_usd, {"gen_ai.request.model": model})
        span.set_attribute("claude.cost_usd", cost_usd)
    if outcome != "ok":
        span.set_status(Status(StatusCode.ERROR, (error or outcome)[:200]))


def record_request(*, endpoint: str, status: str, http_status: int, code: str | None, model: str | None,
                   effort: str | None, latency_ms: int | None, username: str | None, token_prefix: str | None,
                   chat_session_id: str | None) -> None:  # fmt: skip
    """Count a finished API/dashboard request and emit one structured log record for it."""
    instruments().requests.add(
        1, {"endpoint": endpoint, "status": status, "http.response.status_code": http_status, "error.type": code or ""}
    )
    if not _state.enabled:
        return
    fields = {
        "endpoint": endpoint,
        "status": status,
        "http_status": http_status,
        "error_code": code,
        "model": model,
        "effort": effort,
        "latency_ms": latency_ms,
        "user": username,
        "token_prefix": token_prefix,
        "chat_session_id": chat_session_id,
    }
    request_logger.log(
        logging.WARNING if status == "error" else logging.INFO,
        "%s %s %s",
        endpoint,
        http_status,
        status,
        extra={k: v for k, v in fields.items() if v is not None},  # OTel attributes can't be None
    )


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
