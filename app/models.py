import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Only admins sign in to the dashboard; API users authenticate with tokens only.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tokens: Mapped[list["ApiToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="ApiToken.id.desc()"
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Empty list = every enabled model.
    allowed_models: Mapped[list[str]] = mapped_column(JSON, default=list)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer)
    max_effort: Mapped[str | None] = mapped_column(String(16))

    user: Mapped[User] = relationship(back_populates="tokens")

    def status(self, now: datetime | None = None) -> str:
        now = now or utcnow()
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at <= now:
            return "expired"
        return "active"


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Plain ids (no FK) so logs survive user/token deletion.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    token_id: Mapped[int | None] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    token_prefix: Mapped[str | None] = mapped_column(String(16))
    endpoint: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    effort: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)  # ok | error | rejected
    http_status: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    session_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    chat_session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32))  # OpenTelemetry trace, when export is on
    stream: Mapped[bool | None] = mapped_column(Boolean)  # reply sent as server-sent events
    prompt: Mapped[str | None] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class ChatSession(Base):
    """A conversation kept server-side; each turn resumes the matching Claude Code session."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[int | None] = mapped_column(Integer)  # token that created it, if any
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | dashboard
    title: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str] = mapped_column(String(16))
    system_prompt: Mapped[str | None] = mapped_column(Text)
    # Id of the Claude Code transcript this conversation resumes; replaced if it has to be rebuilt.
    claude_session_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    user: Mapped[User] = relationship()
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    model: Mapped[str | None] = mapped_column(String(64))
    effort: Mapped[str | None] = mapped_column(String(16))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    session: Mapped[ChatSession] = relationship(back_populates="messages")
