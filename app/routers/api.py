import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import runtime_settings, telemetry
from app.claude_runner import ClaudeError, ClaudeResult, gate, run_claude, stream_claude
from app.db import get_db, session_factory
from app.models import ApiToken, ChatSession, RequestLog
from app.openai_schemas import (
    ChatCompletionRequest,
    ContentPart,
    ConversionError,
    Effort,
    StreamOptions,
    WebSearchOptions,
    completion_chunk,
    completion_response,
    text_of,
    to_prompt,
)
from app.ratelimit import limiter
from app.request_logs import record_result, save_log
from app.runtime_settings import EFFORTS, effort_rank
from app.security import APIError, authenticate, display_prefix, extract_token
from app.sessions import create_session, delete_session, release_connection, run_turn, session_json, stream_turn

router = APIRouter()

# OpenAI's reasoning_effort values that have no direct Claude Code equivalent.
_REASONING_EFFORT_ALIASES = {"none": "low", "minimal": "low"}


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str | None = None
    effort: Effort | None = None
    reasoning_effort: str | None = None
    system_prompt: str | None = None
    title: str | None = None


class SessionMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str | list[ContentPart]
    model: str | None = None
    effort: Effort | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    web_search_options: WebSearchOptions | None = None


# --- shared helpers ----------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _resolve_effort(effort: str | None, reasoning_effort: Any, default: str) -> str:
    if effort:
        return effort
    if reasoning_effort:
        reasoning = _REASONING_EFFORT_ALIASES.get(reasoning_effort, reasoning_effort)
        if reasoning not in EFFORTS:
            raise APIError(400, f"Invalid reasoning_effort '{reasoning}'. Use one of: {', '.join(EFFORTS)}.")
        return reasoning
    return default


def _check_access(token: ApiToken, settings: dict, model: str, effort: str) -> None:
    if model not in settings["enabled_models"]:
        raise APIError(
            400,
            f"Model '{model}' is not available. Available: {', '.join(settings['enabled_models'])}.",
            code="model_not_found",
        )
    if token.allowed_models and model not in token.allowed_models:
        raise APIError(403, f"This API key may not use model '{model}'.", "permission_error", "model_not_allowed")
    if token.max_effort and effort_rank(effort) > effort_rank(token.max_effort):
        raise APIError(
            403, f"This API key is limited to effort '{token.max_effort}'.", "permission_error", "effort_not_allowed"
        )


def _web_search(settings: dict, options: WebSearchOptions | None) -> bool:
    """Whether this run may search the web: the request asked (`web_search_options`) and the server allows it."""
    if options is None:
        return False
    if not settings["web_search_enabled"]:
        raise APIError(400, "Web search is not enabled on this server.", code="web_search_disabled")
    return True


def _check_rate_limit(token: ApiToken, settings: dict) -> None:
    rpm = token.rate_limit_rpm if token.rate_limit_rpm is not None else settings["default_rate_limit_rpm"]
    retry_after = limiter.hit(token.id, int(rpm or 0))
    if retry_after is not None:
        raise APIError(
            429, f"Rate limit of {rpm} requests/minute exceeded. Retry in {retry_after:.0f}s.", "rate_limit_error",
            "rate_limit_exceeded",
        )  # fmt: skip


async def _parse(request: Request, model_cls: type[BaseModel]) -> tuple[Any, dict]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise APIError(400, "Request body must be valid JSON.") from None
    if not isinstance(raw, dict):
        raise APIError(400, "Request body must be a JSON object.")
    try:
        # reasoning_effort is validated by _resolve_effort, which also maps OpenAI-only values.
        parsed = model_cls.model_validate({k: v for k, v in raw.items() if k != "reasoning_effort"})
    except ValidationError as e:
        raise APIError(400, f"Invalid request: {e.errors(include_url=False)}") from None
    return parsed, raw


def _own_session(db: Session, token: ApiToken, session_id: str) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != token.user_id:
        raise APIError(404, f"Session '{session_id}' not found.", code="session_not_found")
    return session


@dataclass
class SSEStream:
    """A streamed reply, already primed: `_logged` sends it as server-sent events and logs when it ends."""

    body: AsyncIterator[bytes]
    code: str | None = None  # error code for the log, set by the body if it fails part way


Handler = Callable[[ApiToken, dict, RequestLog], Awaitable["dict | SSEStream"]]

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # no proxy buffering


async def _send_stream(stream: SSEStream, log: RequestLog, started: float) -> AsyncIterator[bytes]:
    finished = False
    try:
        async for part in stream.body:
            yield part
        finished = True
    finally:
        if not finished:  # the client went away before the end: the body's cleanup stops Claude
            log.status, log.http_status = "error", 499
            log.error, stream.code = "Client disconnected before the reply finished.", "client_disconnected"
        with anyio.CancelScope(shield=True):
            await stream.body.aclose()
        save_log(log, started, stream.code)


async def _logged(request: Request, db: Session, endpoint: str, handler: Handler, status_code: int = 200):
    """Authenticate, run `handler`, and write one RequestLog row whatever the outcome."""
    started = time.monotonic()
    raw_token = extract_token(request)
    log = RequestLog(
        endpoint=endpoint,
        client_ip=_client_ip(request),
        token_prefix=display_prefix(raw_token) if raw_token else None,
        status="error",
        http_status=500,
    )
    settings = runtime_settings.get_all(db)
    log.trace_id = telemetry.current_trace_id()
    code = None
    streaming = False
    try:
        token = authenticate(db, raw_token)
        log.user_id, log.token_id, log.username = token.user_id, token.id, token.user.username
        out = await handler(token, settings, log)
        if isinstance(out, SSEStream):
            streaming = True
            return StreamingResponse(
                _send_stream(out, log, started), media_type="text/event-stream", headers=_SSE_HEADERS
            )
        return JSONResponse(out, status_code=status_code)
    except APIError as e:
        log.status = "rejected" if e.status < 500 else "error"
        log.http_status, log.error, code = e.status, e.message, e.code
        return JSONResponse(e.body(), status_code=e.status)
    except ClaudeError as e:
        code = "upstream_error" if e.status != 504 else "timeout"
        log.status, log.http_status, log.error = "error", e.status, e.message
        err = APIError(e.status, e.message, "server_error", code)
        return JSONResponse(err.body(), status_code=e.status)
    finally:
        if not streaming:
            save_log(log, started, code, db)


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def _stream_reply(
    events: AsyncIterator["str | ClaudeResult"], *, log: RequestLog, settings: dict, model: str,
    include_usage: bool, prompt_for_log: str | None, session_id: str | None = None, on_close=None,
) -> SSEStream:  # fmt: skip
    """Prime `events` (deltas, then a ClaudeResult) and wrap it as an OpenAI chunk stream.

    Priming runs up to the first text, so anything that fails before output (bad credentials, an unknown
    model, a full queue's timeout) still becomes a normal JSON error with its status code. `on_close(ok)`
    runs when the stream ends, however it ends.
    """
    log.stream = True
    try:
        first = await events.__anext__()
    except BaseException:
        with anyio.CancelScope(shield=True):
            await events.aclose()
        if on_close:
            on_close(False)
        raise
    completion_id, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    stream = SSEStream(body=None)  # type: ignore[arg-type]

    def chunk(**fields) -> bytes:
        body = completion_chunk(completion_id=completion_id, created=created, model=model, **fields)
        if session_id:
            body["session_id"] = session_id
        return _sse(body)

    async def body() -> AsyncIterator[bytes]:
        ok = False
        try:
            yield chunk(delta={"role": "assistant", "content": ""})
            item = first
            while not isinstance(item, ClaudeResult):
                yield chunk(delta={"content": item})
                item = await events.__anext__()
            record_result(log, item, settings, prompt=prompt_for_log)
            ok = True
            yield chunk(delta={}, finish_reason="stop")
            if include_usage:
                yield chunk(usage=item.usage)
            yield b"data: [DONE]\n\n"
        except ClaudeError as e:
            # Headers are sent: report the failure in-band, the way OpenAI does (SDKs raise on it).
            stream.code = "upstream_error" if e.status != 504 else "timeout"
            log.status, log.http_status, log.error = "error", e.status, e.message
            yield _sse(APIError(e.status, e.message, "server_error", stream.code).body())
        finally:
            with anyio.CancelScope(shield=True):
                await events.aclose()
            if on_close:
                on_close(ok)

    stream.body = body()
    return stream


def _authed(db: Session, request: Request) -> ApiToken:
    return authenticate(db, extract_token(request))


async def _session_turn(
    db: Session, token: ApiToken, settings: dict, log: RequestLog, session: ChatSession, content: str,
    model: str, effort: str, stream: StreamOptions | None = None, discard_on_failure: bool = False,
    web_search: bool = False,
) -> "dict | SSEStream":  # fmt: skip
    """Run one session turn. `stream` (the client's stream options) switches to a streamed reply."""
    log.chat_session_id = session.id
    gate.resize(int(settings["max_concurrency"]))
    if stream is not None:
        # The stream outlives this request's DB session, so the turn gets its own.
        sdb = session_factory()
        owned = sdb.get(ChatSession, session.id)
        release_connection(db)

        def on_close(ok: bool) -> None:
            if not ok and discard_on_failure:
                delete_session(sdb, owned)  # a new conversation whose first turn failed
                log.chat_session_id = None
            sdb.close()

        return await _stream_reply(
            stream_turn(sdb, owned, content, model=model, effort=effort, settings=settings, web_search=web_search),
            log=log,
            settings=settings, model=model, include_usage=stream.include_usage, prompt_for_log=content,
            session_id=session.id, on_close=on_close,
        )  # fmt: skip
    result = await run_turn(db, session, content, model=model, effort=effort, settings=settings,
                            web_search=web_search)  # fmt: skip
    record_result(log, result, settings, prompt=content)
    body = completion_response(
        completion_id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=model,
        text=result.text,
        usage=result.usage,
        web_searches=result.web_searches,
    )
    body["session_id"] = session.id
    return body


# --- chat completions --------------------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, db: Session = Depends(get_db)):
    async def handle(token: ApiToken, settings: dict, log: RequestLog) -> dict:
        req, raw = await _parse(request, ChatCompletionRequest)
        model = req.model or settings["default_model"]
        effort = _resolve_effort(req.effort, raw.get("reasoning_effort"), settings["default_effort"])
        log.model, log.effort = model, effort

        stream = (req.stream_options or StreamOptions()) if req.stream else None
        _check_access(token, settings, model, effort)
        web_search = _web_search(settings, req.web_search_options)
        _check_rate_limit(token, settings)

        try:
            system, prompt = to_prompt(req.messages, req.response_format)
        except ConversionError as e:
            raise APIError(400, str(e)) from None

        if req.session_id:
            # Server-side history: only the newest user message is sent.
            content = text_of(req.messages[-1].content)
            if req.session_id != "new":
                session = _own_session(db, token, req.session_id)
                return await _session_turn(
                    db, token, settings, log, session, content, model, effort, stream, web_search=web_search
                )
            session = create_session(
                db, user=token.user, model=model, effort=effort, system_prompt=system, token_id=token.id
            )
            if stream is not None:  # the stream deletes the session itself if its first turn fails
                return await _session_turn(
                    db, token, settings, log, session, content, model, effort, stream, discard_on_failure=True,
                    web_search=web_search,
                )  # fmt: skip
            try:
                return await _session_turn(
                    db, token, settings, log, session, content, model, effort, web_search=web_search
                )
            except ClaudeError:
                delete_session(db, session)  # don't leave an empty conversation behind
                log.chat_session_id = None
                raise

        system = system or settings["default_system_prompt"]
        json_schema = None
        if req.response_format and req.response_format.type == "json_schema":
            spec = req.response_format.json_schema or {}
            json_schema = spec.get("schema", spec)

        gate.resize(int(settings["max_concurrency"]))
        release_connection(db)
        run_args = {
            "model": model,
            "effort": effort,
            "system_prompt": system,
            "timeout_s": float(settings["request_timeout_s"]),
            "json_schema": json_schema,
            "max_budget_usd": settings["max_budget_usd_per_request"],
            "web_search": web_search,
        }
        logged_prompt = f"[system]\n{system}\n\n[prompt]\n{prompt}"
        if stream is not None:
            return await _stream_reply(
                stream_claude(prompt, **run_args), log=log, settings=settings, model=model,
                include_usage=stream.include_usage, prompt_for_log=logged_prompt,
            )  # fmt: skip
        result = await run_claude(prompt, **run_args)
        record_result(log, result, settings, prompt=logged_prompt)
        return completion_response(
            completion_id=f"chatcmpl-{result.session_id or uuid.uuid4().hex}",
            created=int(time.time()),
            model=model,
            text=result.text,
            usage=result.usage,
            web_searches=result.web_searches,
        )

    return await _logged(request, db, "chat.completions", handle)


# --- sessions ----------------------------------------------------------------


@router.post("/v1/sessions")
async def sessions_create(request: Request, db: Session = Depends(get_db)):
    try:
        token = _authed(db, request)
        req, raw = await _parse(request, SessionCreate)
        settings = runtime_settings.get_all(db)
        model = req.model or settings["default_model"]
        effort = _resolve_effort(req.effort, raw.get("reasoning_effort"), settings["default_effort"])
        _check_access(token, settings, model, effort)
        session = create_session(
            db, user=token.user, model=model, effort=effort, system_prompt=req.system_prompt, title=req.title,
            token_id=token.id,
        )  # fmt: skip
        return JSONResponse(session_json(session), status_code=201)
    except APIError as e:
        return JSONResponse(e.body(), status_code=e.status)


@router.get("/v1/sessions")
def sessions_list(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    try:
        token = _authed(db, request)
    except APIError as e:
        return JSONResponse(e.body(), status_code=e.status)
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.user_id == token.user_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {"object": "list", "data": [session_json(s) for s in sessions]}


@router.get("/v1/sessions/{session_id}")
def sessions_get(session_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        return session_json(_own_session(db, _authed(db, request), session_id), with_messages=True)
    except APIError as e:
        return JSONResponse(e.body(), status_code=e.status)


@router.delete("/v1/sessions/{session_id}")
def sessions_delete(session_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        session = _own_session(db, _authed(db, request), session_id)
    except APIError as e:
        return JSONResponse(e.body(), status_code=e.status)
    delete_session(db, session)
    return {"id": session_id, "object": "chat.session.deleted", "deleted": True}


@router.post("/v1/sessions/{session_id}/messages")
async def sessions_message(session_id: str, request: Request, db: Session = Depends(get_db)):
    async def handle(token: ApiToken, settings: dict, log: RequestLog) -> dict:
        session = _own_session(db, token, session_id)
        req, raw = await _parse(request, SessionMessage)
        model = req.model or session.model
        effort = _resolve_effort(req.effort, raw.get("reasoning_effort"), session.effort)
        log.model, log.effort = model, effort
        _check_access(token, settings, model, effort)
        web_search = _web_search(settings, req.web_search_options)
        _check_rate_limit(token, settings)
        try:
            content = text_of(req.content)
        except ConversionError as e:
            raise APIError(400, str(e)) from None
        if not content.strip():
            raise APIError(400, "Message content is empty.")
        stream = (req.stream_options or StreamOptions()) if req.stream else None
        return await _session_turn(
            db, token, settings, log, session, content, model, effort, stream, web_search=web_search
        )

    return await _logged(request, db, "sessions.messages", handle)


# --- misc --------------------------------------------------------------------


@router.get("/v1/models")
def list_models(request: Request, db: Session = Depends(get_db)):
    try:
        token = _authed(db, request)
    except APIError as e:
        return JSONResponse(e.body(), status_code=e.status)
    models = runtime_settings.get(db, "enabled_models")
    if token.allowed_models:
        models = [m for m in models if m in token.allowed_models]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": "anthropic"} for m in models],
    }


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
