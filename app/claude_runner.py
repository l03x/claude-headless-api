"""Runs one completion through Claude Code's headless mode (`claude -p`)."""

import asyncio
import json
import os
import signal
import tempfile
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import telemetry
from app.config import get_settings

# Environment variables forwarded to the CLI; everything else in the server's env stays out.
_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
)

_ERR_EXCERPT = 2000
# stream-json puts the whole reply on its final `result` line; asyncio's default 64 KiB line limit is too small.
_STREAM_LINE_LIMIT = 16 * 1024 * 1024
# Each search costs the model a turn (call, then read the results); this bounds a run that keeps searching.
WEB_SEARCH_MAX_TURNS = 10


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    web_searches: int | None = None


class ClaudeError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class ConcurrencyGate:
    """A semaphore whose size can change at runtime and which reports its own load.

    Releasing never awaits. A streamed reply is cancelled by anyio when its client disconnects, and anyio
    re-raises the cancellation at every await during cleanup, so an awaiting release could lose its slot
    for good and the gate would slowly fill up.
    """

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.in_flight = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def resize(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._wake()

    def _wake(self) -> None:
        # Hand free slots to waiters in arrival order; the slot is counted as theirs right away.
        while self._waiters and self.in_flight < self.limit:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.in_flight += 1
                waiter.set_result(None)

    async def __aenter__(self) -> None:
        if self.in_flight < self.limit and not self._waiters:
            self.in_flight += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.in_flight -= 1  # a slot was handed over just as we gave up: pass it on
                self._wake()
            elif waiter in self._waiters:
                self._waiters.remove(waiter)
            raise

    async def __aexit__(self, *exc) -> None:
        self.in_flight -= 1
        self._wake()


gate = ConcurrencyGate(4)


def _data_dir() -> Path:
    # Absolute on purpose: the CLI runs in another cwd and would resolve a relative CLAUDE_CONFIG_DIR
    # against it, filing transcripts where transcript_paths() never looks (so sessions never resume).
    return Path(get_settings().claude_data_dir).resolve()


def config_dir() -> Path:
    path = _data_dir() / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_workdir() -> Path:
    # Claude Code files transcripts under the cwd, so resumable runs all share one fixed cwd.
    path = _data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transcript_paths(claude_session_id: str) -> list[Path]:
    try:
        uuid.UUID(claude_session_id)
    except ValueError:
        return []
    return list((config_dir() / "projects").glob(f"*/{claude_session_id}.jsonl"))


def delete_transcript(claude_session_id: str | None) -> None:
    if not claude_session_id:
        return
    for path in transcript_paths(claude_session_id):
        path.unlink(missing_ok=True)


def build_argv(
    *,
    model: str,
    effort: str,
    system_prompt: str,
    json_schema: dict | None = None,
    max_budget_usd: float | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    stream: bool = False,
    web_search: bool = False,
) -> list[str]:
    # stream-json needs --verbose in print mode; --include-partial-messages adds the token-level deltas.
    output = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"] if stream else [
        "--output-format", "json"]  # fmt: skip
    argv = [
        get_settings().claude_bin,
        "-p",
        *output,
        "--model", model,
        "--effort", effort,
        *_tool_args(web_search),
        "--system-prompt", system_prompt,
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]  # fmt: skip
    if resume:
        argv += ["--resume", resume]
    elif session_id:
        argv += ["--session-id", session_id]
    else:
        argv.append("--no-session-persistence")
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema)]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


def _tool_args(web_search: bool) -> list[str]:
    if not web_search:
        return ["--tools", ""]
    # WebSearch runs on Anthropic's side, so it reaches nothing on this host's network (WebFetch would).
    # Print mode has nobody to answer a permission prompt, so the tool must be pre-approved.
    return ["--tools", "WebSearch", "--allowedTools", "WebSearch", "--max-turns", str(WEB_SEARCH_MAX_TURNS)]


def _child_env() -> dict[str, str]:
    settings = get_settings()
    extra = tuple(n.strip() for n in settings.claude_env_passthrough.split(",") if n.strip())
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH + extra if k in os.environ}
    token = settings.claude_code_oauth_token
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir())
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_claude(
    prompt: str,
    *,
    model: str,
    effort: str,
    system_prompt: str,
    timeout_s: float,
    json_schema: dict | None = None,
    max_budget_usd: float | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    web_search: bool = False,
) -> ClaudeResult:
    """Run one turn. `session_id` starts a persisted session with that id; `resume` continues one.

    Without either, the run is stateless and leaves nothing on disk.
    """
    argv = build_argv(
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        json_schema=json_schema,
        max_budget_usd=max_budget_usd,
        session_id=session_id,
        resume=resume,
        web_search=web_search,
    )
    persistent = bool(session_id or resume)
    mode = "resume" if resume else "new" if session_id else "stateless"
    with telemetry.claude_span(model, effort, mode) as span:
        queued_at = time.monotonic()
        queue_span = telemetry.tracer().start_span("claude.queue_wait")
        queue_wait = run_s = None
        result, outcome, error = None, "error", None
        try:
            async with gate:
                queue_wait = time.monotonic() - queued_at
                queue_span.end()
                started = time.monotonic()
                with telemetry.tracer().start_as_current_span("claude.process"):
                    returncode, stdout, stderr = await _run_process(argv, prompt, persistent, timeout_s)
                run_s = time.monotonic() - started
            result = parse_output(returncode, stdout, stderr)
            outcome = "ok"
            return result
        except ClaudeError as e:
            outcome, error = ("timeout" if e.status == 504 else "error"), e.message
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if queue_wait is None:  # cancelled or failed while still queued
                queue_wait = time.monotonic() - queued_at
                queue_span.end()
            telemetry.record_run(
                span,
                model=model,
                effort=effort,
                mode=mode,
                outcome=outcome,
                queue_wait_s=queue_wait,
                run_s=run_s,
                usage=result.usage if result else None,
                cost_usd=result.cost_usd if result else None,
                web_searches=result.web_searches if result else None,
                error=error,
            )


async def _run_process(
    argv: list[str], prompt: str, persistent: bool, timeout_s: float
) -> tuple[int | None, bytes, bytes]:
    # Stateless runs get a fresh empty cwd so nothing is picked up or left behind.
    if persistent:
        workdir = nullcontext(str(sessions_workdir()))
    else:
        workdir = tempfile.TemporaryDirectory(prefix="claude-req-")
    with workdir as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=_child_env(),
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise ClaudeError(f"Claude CLI not found: {argv[0]}", 500) from e
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout_s)
        except asyncio.TimeoutError:
            _kill(proc)
            await proc.wait()
            raise ClaudeError(f"Claude did not answer within {timeout_s:g}s.", 504) from None
        except asyncio.CancelledError:
            _kill(proc)
            raise
    return proc.returncode, stdout, stderr


def parse_output(returncode: int | None, stdout: bytes, stderr: bytes) -> ClaudeResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = (stderr or stdout).decode(errors="replace").strip()[:_ERR_EXCERPT]
        raise ClaudeError(f"Claude CLI exited with code {returncode}: {detail or 'no output'}") from None
    if not isinstance(data, dict):
        raise ClaudeError("Claude CLI returned unexpected output.")
    return result_from(data, returncode)


def result_from(data: dict, returncode: int | None) -> ClaudeResult:
    """Build the result from the CLI's final JSON object (the whole json output, or stream-json's `result`)."""
    if data.get("is_error") or returncode != 0:
        detail = str(data.get("result") or data.get("subtype") or "unknown error")[:_ERR_EXCERPT]
        raise ClaudeError(f"Claude CLI error: {detail}")

    structured = data.get("structured_output")
    text = json.dumps(structured) if structured is not None else str(data.get("result") or "")
    usage = data.get("usage") or {}
    return ClaudeResult(
        text=text,
        session_id=data.get("session_id"),
        usage=usage,
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        stop_reason=data.get("stop_reason"),
        web_searches=_web_searches(data, usage),
    )


def _web_searches(data: dict, usage: dict) -> int | None:
    """Searches the run made: `usage.server_tool_use`, else the per-model totals in `modelUsage`."""
    server = usage.get("server_tool_use")
    if isinstance(server, dict) and isinstance(server.get("web_search_requests"), int):
        return server["web_search_requests"]
    per_model = data.get("modelUsage")
    if isinstance(per_model, dict):
        counts = [m.get("webSearchRequests") for m in per_model.values() if isinstance(m, dict)]
        counts = [n for n in counts if isinstance(n, int)]
        if counts:
            return sum(counts)
    return None


async def stream_claude(
    prompt: str,
    *,
    model: str,
    effort: str,
    system_prompt: str,
    timeout_s: float,
    json_schema: dict | None = None,
    max_budget_usd: float | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    web_search: bool = False,
) -> AsyncIterator["str | ClaudeResult"]:
    """Like run_claude, but yields the reply's text deltas as they arrive, then one ClaudeResult.

    Errors raise ClaudeError, before or after some text. If the consumer stops early (cancellation, or
    closing the generator on client disconnect) the CLI process group is killed.
    """
    argv = build_argv(
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        json_schema=json_schema,
        max_budget_usd=max_budget_usd,
        session_id=session_id,
        resume=resume,
        stream=True,
        web_search=web_search,
    )
    persistent = bool(session_id or resume)
    mode = "resume" if resume else "new" if session_id else "stateless"
    span = telemetry.start_claude_span(model, effort, mode)
    queued_at = time.monotonic()
    queue_span = telemetry.child_span(span, "claude.queue_wait")
    queue_wait = run_s = None
    result, outcome, error = None, "cancelled", None
    try:
        async with gate:
            queue_wait = time.monotonic() - queued_at
            queue_span.end()
            started = time.monotonic()
            process_span = telemetry.child_span(span, "claude.process")
            first = True
            try:
                events = _stream_process(argv, prompt, persistent, timeout_s, structured=json_schema is not None)
                async with aclosing(events):
                    async for item in events:
                        if isinstance(item, ClaudeResult):
                            result = item
                            continue
                        if first:
                            telemetry.record_ttft(model, time.monotonic() - started)
                            first = False
                        yield item
            finally:
                process_span.end()
            run_s = time.monotonic() - started
        if result is None:
            raise ClaudeError("Claude CLI ended without a result.")
        outcome = "ok"
        yield result
    except ClaudeError as e:
        outcome, error = ("timeout" if e.status == 504 else "error"), e.message
        raise
    finally:
        if queue_wait is None:  # cancelled or failed while still queued
            queue_wait = time.monotonic() - queued_at
            queue_span.end()
        telemetry.record_run(
            span,
            model=model,
            effort=effort,
            mode=mode,
            outcome=outcome,
            queue_wait_s=queue_wait,
            run_s=run_s,
            usage=result.usage if result else None,
            cost_usd=result.cost_usd if result else None,
            web_searches=result.web_searches if result else None,
            error=error,
        )
        span.end()


def _message_start(event: dict) -> bool:
    """Whether a stream-json event opens a new top-level assistant message (one per model turn)."""
    if event.get("type") != "stream_event" or event.get("parent_tool_use_id") is not None:
        return False
    return (event.get("event") or {}).get("type") == "message_start"


def _text_delta(event: dict) -> str | None:
    """The text of a stream-json `stream_event` carrying a text delta, else None.

    Thinking deltas and anything from a sub-agent (non-null parent_tool_use_id) are skipped.
    """
    if event.get("type") != "stream_event" or event.get("parent_tool_use_id") is not None:
        return None
    inner = event.get("event") or {}
    delta = inner.get("delta") or {}
    if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
        return delta.get("text") or None
    return None


async def _stream_process(
    argv: list[str], prompt: str, persistent: bool, timeout_s: float, *, structured: bool
) -> AsyncIterator["str | ClaudeResult"]:
    if persistent:
        workdir = nullcontext(str(sessions_workdir()))
    else:
        workdir = tempfile.TemporaryDirectory(prefix="claude-req-")
    with workdir as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=_child_env(),
                start_new_session=True,
                limit=_STREAM_LINE_LIMIT,
            )
        except FileNotFoundError as e:
            raise ClaudeError(f"Claude CLI not found: {argv[0]}", 500) from e
        stderr_task = asyncio.ensure_future(proc.stderr.read())  # drained alongside, so the pipe never fills
        deadline = time.monotonic() + timeout_s
        final: dict | None = None
        streamed: list[str] = []
        new_message = joined = False
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result":
                    final = event
                elif _message_start(event):
                    new_message = True
                elif not structured:  # structured output is only meaningful once complete
                    text = _text_delta(event)
                    if text:
                        if new_message and streamed:
                            # A tool call split the reply into turns ("Let me search…", then the answer).
                            streamed.append("\n\n")
                            joined = True
                            yield "\n\n"
                        new_message = False
                        streamed.append(text)
                        yield text
            await asyncio.wait_for(proc.wait(), max(0.1, deadline - time.monotonic()))
            stderr = await asyncio.wait_for(stderr_task, 5)
        except asyncio.TimeoutError:
            raise ClaudeError(f"Claude did not answer within {timeout_s:g}s.", 504) from None
        finally:
            # Synchronous steps first: under anyio cancellation every await here can raise again.
            running = proc.returncode is None
            if running:  # timeout, error, or the consumer went away
                _kill(proc)
            if not stderr_task.done():
                stderr_task.cancel()
            if running:
                await proc.wait()

    if final is None:
        detail = stderr.decode(errors="replace").strip()[:_ERR_EXCERPT]
        raise ClaudeError(f"Claude CLI exited with code {proc.returncode}: {detail or 'no output'}")
    result = result_from(final, proc.returncode)
    if not streamed and result.text:
        yield result.text  # no deltas arrived (structured output, or a CLI that only sent `result`)
    if joined:
        # `result` holds only the last turn's text. Keep what the client was shown, so a stored session
        # message reads the same after a reload as it did while streaming.
        result.text = "".join(streamed)
    yield result


async def claude_version() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            get_settings().claude_bin,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return out.decode(errors="replace").strip()
    except Exception as e:  # noqa: BLE001 - shown as-is on the System page
        return f"unavailable ({e})"
