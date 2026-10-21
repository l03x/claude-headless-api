"""Runs one completion through Claude Code's headless mode (`claude -p`)."""

import asyncio
import json
import os
import signal
import tempfile
import time
import uuid
from contextlib import nullcontext
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


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None


class ClaudeError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class ConcurrencyGate:
    """A semaphore whose size can change at runtime and which reports its own load."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.in_flight = 0
        self.queued = 0
        self._cond: asyncio.Condition | None = None

    def _condition(self) -> asyncio.Condition:
        # Created lazily so it binds to the running loop (tests spin up several).
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def resize(self, limit: int) -> None:
        self.limit = max(1, limit)

    async def __aenter__(self) -> None:
        cond = self._condition()
        async with cond:
            self.queued += 1
            try:
                await cond.wait_for(lambda: self.in_flight < self.limit)
            finally:
                self.queued -= 1
            self.in_flight += 1

    async def __aexit__(self, *exc) -> None:
        cond = self._condition()
        async with cond:
            self.in_flight -= 1
            cond.notify_all()


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
) -> list[str]:
    argv = [
        get_settings().claude_bin,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--effort", effort,
        "--tools", "",
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

    if data.get("is_error") or returncode != 0:
        detail = str(data.get("result") or data.get("subtype") or "unknown error")[:_ERR_EXCERPT]
        raise ClaudeError(f"Claude CLI error: {detail}")

    structured = data.get("structured_output")
    text = json.dumps(structured) if structured is not None else str(data.get("result") or "")
    return ClaudeResult(
        text=text,
        session_id=data.get("session_id"),
        usage=data.get("usage") or {},
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        stop_reason=data.get("stop_reason"),
    )


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
