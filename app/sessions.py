"""Server-side conversations backed by Claude Code's own sessions.

The first turn runs `claude -p --session-id <uuid>`; later turns run `--resume <uuid>`, so Claude
keeps the real conversation (and its prompt cache). Every turn is also stored in `chat_messages`
for the dashboard and API, and that copy rebuilds the conversation if the transcript is gone.
"""

import asyncio
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.claude_runner import ClaudeResult, delete_transcript, run_claude, transcript_paths
from app.models import ChatMessage, ChatSession, User, utcnow
from app.openai_schemas import render_transcript

_TITLE_LEN = 80

# One turn at a time per conversation: two concurrent resumes would fork the transcript.
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def create_session(
    db: Session,
    *,
    user: User,
    model: str,
    effort: str,
    system_prompt: str | None = None,
    title: str | None = None,
    source: str = "api",
    token_id: int | None = None,
) -> ChatSession:
    session = ChatSession(
        user_id=user.id,
        token_id=token_id,
        source=source,
        title=(title or "").strip()[:_TITLE_LEN] or None,
        model=model,
        effort=effort,
        system_prompt=(system_prompt or "").strip() or None,
    )
    db.add(session)
    db.commit()
    return session


async def run_turn(
    db: Session,
    session: ChatSession,
    content: str,
    *,
    model: str,
    effort: str,
    settings: dict,
) -> ClaudeResult:
    """Send one user message in `session` and persist both sides of the exchange."""
    async with _locks[session.id]:
        db.refresh(session)
        resume = session.claude_session_id
        if resume and not transcript_paths(resume):
            resume = None  # transcript lost (volume reset, manual cleanup): rebuild from our copy
        new_id = None if resume else str(uuid.uuid4())
        if resume:
            prompt = content
        else:
            history = [(m.role, m.content) for m in session.messages]
            prompt = render_transcript(history, content)

        started = time.monotonic()
        result = await run_claude(
            prompt,
            model=model,
            effort=effort,
            system_prompt=session.system_prompt or settings["default_system_prompt"],
            timeout_s=float(settings["request_timeout_s"]),
            max_budget_usd=settings["max_budget_usd_per_request"],
            session_id=new_id,
            resume=resume,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        now = utcnow()
        usage = result.usage
        tin, tout = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        session.claude_session_id = result.session_id or new_id or resume
        db.add(ChatMessage(session_id=session.id, role="user", content=content, created_at=now))
        db.add(
            ChatMessage(
                session_id=session.id,
                role="assistant",
                content=result.text,
                created_at=now,
                model=model,
                effort=effort,
                input_tokens=tin,
                output_tokens=tout,
                cost_usd=result.cost_usd,
                latency_ms=latency_ms,
            )
        )
        session.model, session.effort = model, effort
        session.updated_at = now
        session.message_count += 2
        session.input_tokens += tin
        session.output_tokens += tout
        session.total_cost_usd += result.cost_usd or 0.0
        if not session.title:
            session.title = " ".join(content.split())[:_TITLE_LEN] or "Untitled"
        db.commit()
        return result


def delete_session(db: Session, session: ChatSession) -> None:
    delete_transcript(session.claude_session_id)
    _locks.pop(session.id, None)
    db.delete(session)
    db.commit()


def delete_user_transcripts(db: Session, user_id: int) -> None:
    """Remove Claude transcripts for a user about to be deleted (DB rows go by cascade)."""
    for (claude_id,) in db.query(ChatSession.claude_session_id).filter(ChatSession.user_id == user_id).all():
        delete_transcript(claude_id)


def purge_stale_sessions(db: Session, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    stale = db.query(ChatSession).filter(ChatSession.updated_at < cutoff).all()
    for session in stale:
        delete_session(db, session)
    return len(stale)


def _ts(value: datetime) -> int:
    return int(value.replace(tzinfo=timezone.utc).timestamp())  # stored as naive UTC


def session_json(session: ChatSession, with_messages: bool = False) -> dict:
    body = {
        "id": session.id,
        "object": "chat.session",
        "title": session.title,
        "model": session.model,
        "effort": session.effort,
        "system_prompt": session.system_prompt,
        "created_at": _ts(session.created_at),
        "updated_at": _ts(session.updated_at),
        "message_count": session.message_count,
        "usage": {"input_tokens": session.input_tokens, "output_tokens": session.output_tokens},
        "total_cost_usd": round(session.total_cost_usd, 6),
    }
    if with_messages:
        body["messages"] = [
            {"role": m.role, "content": m.content, "created_at": _ts(m.created_at)}
            for m in session.messages
        ]
    return body
