"""Filling `RequestLog` rows, shared by the API and the dashboard chat."""

import time

from sqlalchemy.orm import Session

from app import telemetry
from app.claude_runner import ClaudeResult
from app.db import session_factory
from app.models import RequestLog


def save_log(log: RequestLog, started: float, code: str | None, db: Session | None = None) -> None:
    """Write the request's log row (with its latency) and report it to telemetry.

    Streamed replies outlive the request's own DB session, so they pass no `db` and get a fresh one.
    """
    log.latency_ms = int((time.monotonic() - started) * 1000)
    if db is None:
        with session_factory() as fresh:
            fresh.add(log)
            fresh.commit()
    else:
        db.add(log)
        db.commit()
    telemetry.record_request(log, code=code)


def record_result(log: RequestLog, result: ClaudeResult, settings: dict, prompt: str | None = None) -> None:
    usage = result.usage
    log.status, log.http_status = "ok", 200
    log.session_id = result.session_id
    log.input_tokens = usage.get("input_tokens")
    log.output_tokens = usage.get("output_tokens")
    log.cache_read_tokens = usage.get("cache_read_input_tokens")
    log.cache_creation_tokens = usage.get("cache_creation_input_tokens")
    log.cost_usd = result.cost_usd
    if settings["log_bodies"]:
        if prompt is not None:
            log.prompt = prompt
        log.response = result.text
