import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import db as app_db
from app import telemetry
from app.models import RequestLog

SECRET = "SECRET-PROMPT-7f3a"


@pytest.fixture
def otel(client, monkeypatch):
    """A second app on the same test database, with telemetry going to in-memory exporters."""
    from app.main import create_app

    monkeypatch.delenv("OTEL_SDK_DISABLED")  # the SDK itself honours it and would hand out no-op providers
    app = create_app()
    spans, reader, logs = InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogExporter()
    assert telemetry.setup(app, app_db.get_engine(), span_exporter=spans, metric_reader=reader, log_exporter=logs)
    with TestClient(app) as c:
        yield c, spans, reader, logs
    assert not telemetry.enabled()  # lifespan shutdown flushed and undid everything


def H(raw):
    return {"Authorization": f"Bearer {raw}"}


def finished(spans):
    telemetry.force_flush()
    return spans.get_finished_spans()


def metric_points(reader, name):
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return list(m.data.data_points)
    return []


def test_disabled_without_endpoint(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup(FastAPI(), None) is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://alloy:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert telemetry.setup(FastAPI(), None) is False
    assert not telemetry.enabled()


def test_chat_trace_structure(otel, make_token, db):
    c, spans, _, _ = otel
    raw, _ = make_token()
    r = c.post("/v1/chat/completions", json={"model": "haiku", "effort": "high", "messages": [{"role": "user", "content": "hi"}]}, headers=H(raw))
    assert r.status_code == 200
    by_name = {s.name: s for s in finished(spans)}

    chat = by_name["chat haiku"]
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.request.model"] == "haiku"
    assert chat.attributes["claude.effort"] == "high"
    assert chat.attributes["claude.session_mode"] == "stateless"
    assert chat.attributes["claude.outcome"] == "ok"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 10 and chat.attributes["gen_ai.usage.output_tokens"] == 5
    assert chat.attributes["claude.cost_usd"] == pytest.approx(0.0123)

    for child in ("claude.queue_wait", "claude.process"):
        assert by_name[child].parent.span_id == chat.context.span_id
    server = by_name["POST /v1/chat/completions"]
    assert chat.context.trace_id == server.context.trace_id
    assert any(any(k.startswith("db.") for k in s.attributes) for s in by_name.values()), "no SQL spans"
    assert "connect" not in by_name and not [n for n in by_name if n.endswith((" http receive", " http send"))]
    sql = [s for s in finished(spans) if any(k.startswith("db.") for k in s.attributes)]
    assert all(s.parent is not None for s in sql), "SQL spans outside a request should be dropped"

    # The request log row points at the trace.
    log = db.query(RequestLog).order_by(RequestLog.id.desc()).first()
    assert log.trace_id == format(chat.context.trace_id, "032x")


def test_metrics_and_logs(otel, make_token):
    c, _, reader, logs = otel
    raw, _ = make_token()
    c.post("/v1/chat/completions", json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}, headers=H(raw))
    c.post("/v1/chat/completions", json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]}, headers=H(raw))

    runs = metric_points(reader, "claude.runs")
    assert sum(p.value for p in runs if p.attributes["outcome"] == "ok") == 1
    tokens = {p.attributes["gen_ai.token.type"]: p.value for p in metric_points(reader, "claude.tokens")}
    assert tokens == {"input": 10, "output": 5, "cache_read": 100, "cache_creation": 3}
    assert metric_points(reader, "claude.cost")[0].value == pytest.approx(0.0123)
    assert metric_points(reader, "claude.run.duration") and metric_points(reader, "claude.queue.wait")
    requests = {(p.attributes["status"], p.attributes["error.type"]): p.value for p in metric_points(reader, "api.requests")}
    assert requests == {("ok", ""): 1, ("rejected", "model_not_found"): 1}
    for gauge in ("claude.in_flight", "claude.queued", "claude.concurrency_limit"):
        assert metric_points(reader, gauge), gauge

    telemetry.force_flush()
    records = [entry.log_record for entry in logs.get_finished_logs()]
    request_records = [r for r in records if r.attributes and r.attributes.get("endpoint") == "chat.completions"]
    assert len(request_records) == 2
    ok = next(r for r in request_records if r.attributes["status"] == "ok")
    assert ok.attributes["model"] == "haiku" and ok.attributes["user"] == "alice" and ok.trace_id


def test_failed_run_marks_span_error(otel, make_token, monkeypatch):
    c, spans, reader, _ = otel
    raw, _ = make_token()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    assert c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}, headers=H(raw)).status_code == 502
    chat = next(s for s in finished(spans) if s.name.startswith("chat "))
    assert chat.status.status_code == StatusCode.ERROR and chat.attributes["claude.outcome"] == "error"
    assert sum(p.value for p in metric_points(reader, "claude.runs") if p.attributes["outcome"] == "error") == 1


def test_session_modes(otel, make_token):
    c, spans, _, _ = otel
    raw, _ = make_token()
    sid = c.post("/v1/sessions", json={"model": "haiku"}, headers=H(raw)).json()["id"]
    for text in ("one", "two"):
        assert c.post(f"/v1/sessions/{sid}/messages", json={"content": text}, headers=H(raw)).status_code == 200
    modes = [s.attributes["claude.session_mode"] for s in finished(spans) if s.name == "chat haiku"]
    assert modes == ["new", "resume"]


def test_health_checks_are_not_traced(otel):
    c, spans, _, _ = otel
    c.get("/healthz")
    assert not [s for s in finished(spans) if "healthz" in s.name]


def test_no_prompt_or_reply_content_in_telemetry(otel, make_token, monkeypatch):
    c, spans, reader, logs = otel
    raw, _ = make_token()
    body = {"model": "haiku", "messages": [{"role": "system", "content": SECRET}, {"role": "user", "content": SECRET}]}
    reply = c.post("/v1/chat/completions", json=body, headers=H(raw)).json()["choices"][0]["message"]["content"]
    assert SECRET in reply  # the fake echoes the prompt, so the reply carries it too
    sid = c.post("/v1/sessions", json={"system_prompt": SECRET}, headers=H(raw)).json()["id"]
    c.post(f"/v1/sessions/{sid}/messages", json={"content": SECRET}, headers=H(raw))

    dumped = [s.to_json() for s in finished(spans)]
    dumped += [str(e.log_record.body) + json.dumps(dict(e.log_record.attributes or {}), default=str) for e in logs.get_finished_logs()]
    dumped.append(reader.get_metrics_data().to_json())
    leaks = [d[:200] for d in dumped if SECRET in d]
    assert not leaks, leaks
