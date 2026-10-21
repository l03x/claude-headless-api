"""Filling `RequestLog` rows, shared by the API and the dashboard chat."""

from app.claude_runner import ClaudeResult
from app.models import RequestLog


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
