import csv
import os
import io
import math
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import runtime_settings, telemetry
from app.claude_runner import ClaudeError, claude_version, gate, run_claude
from app.config import get_settings
from app.db import get_db
from app.models import ApiToken, ChatSession, RequestLog, User, utcnow
from app.request_logs import record_result
from app.runtime_settings import EFFORTS
from app.security import (
    csrf_protect,
    csrf_token,
    display_prefix,
    generate_token,
    hash_password,
    hash_token,
    require_admin,
    verify_password,
)
from app.sessions import create_session, delete_session, delete_user_transcripts, release_connection, run_turn

router = APIRouter(prefix="/admin", include_in_schema=False)
post = lambda path: router.post(path, dependencies=[Depends(csrf_protect)])  # noqa: E731

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
templates.env.globals["EFFORTS"] = EFFORTS


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def _fmt_cost(value: float | None) -> str:
    return f"${value:.4f}" if value is not None else "—"


def _fmt_int(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["cost"] = _fmt_cost
templates.env.filters["num"] = _fmt_int

PAGE_SIZE = 50


# --- helpers -----------------------------------------------------------------


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def render(request: Request, name: str, admin: User | None = None, status_code: int = 200, **ctx) -> HTMLResponse:
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request,
        name,
        {"admin": admin, "csrf": csrf_token(request), "messages": messages, **ctx},
        status_code=status_code,
    )


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _opt_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def _opt_float(value: str | None) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def _end_of_day(value: str | None) -> datetime | None:
    value = (value or "").strip()
    return datetime.combine(date.fromisoformat(value), dtime(23, 59, 59)) if value else None


def _admin_count(db: Session) -> int:
    return db.query(User).filter(User.is_admin.is_(True), User.is_active.is_(True)).count()


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404)
    return obj


# --- auth --------------------------------------------------------------------


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/admin") and not value.startswith("//") else "/admin"


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", next=_safe_next(next))


@post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    user = db.query(User).filter(User.username == username).one_or_none()
    if (
        user is None
        or not user.is_admin
        or not user.is_active
        or not verify_password(user.password_hash, str(form.get("password", "")))
    ):
        flash(request, "Invalid username or password.", "error")
        return render(request, "login.html", next=_safe_next(form.get("next")), status_code=401)
    request.session.clear()
    request.session["admin_id"] = user.id
    return back(_safe_next(form.get("next")))


@post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/admin/login")


# --- overview ----------------------------------------------------------------


@router.get("")
def overview(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    now = utcnow()

    def window_stats(since: datetime) -> dict:
        q = db.query(
            func.count(RequestLog.id),
            func.sum(RequestLog.status == "ok"),
            func.sum(RequestLog.status != "ok"),
            func.sum(RequestLog.cost_usd),
            func.sum(RequestLog.input_tokens),
            func.sum(RequestLog.output_tokens),
            func.avg(RequestLog.latency_ms).filter(RequestLog.status == "ok"),
        ).filter(RequestLog.created_at >= since)
        total, ok, failed, cost, tin, tout, lat = q.one()
        return {
            "total": total or 0,
            "ok": ok or 0,
            "failed": failed or 0,
            "cost": cost or 0.0,
            "input_tokens": tin or 0,
            "output_tokens": tout or 0,
            "avg_latency": int(lat) if lat else None,
        }

    day = window_stats(now - timedelta(days=1))
    week = window_stats(now - timedelta(days=7))

    # Requests per day for the last 14 days.
    start = (now - timedelta(days=13)).date()
    rows = (
        db.query(func.date(RequestLog.created_at), func.count(RequestLog.id), func.sum(RequestLog.status != "ok"))
        .filter(RequestLog.created_at >= datetime.combine(start, dtime.min))
        .group_by(func.date(RequestLog.created_at))
        .all()
    )
    by_day = {str(d): (n, f or 0) for d, n, f in rows}
    series = []
    for i in range(14):
        d = start + timedelta(days=i)
        n, f = by_day.get(d.isoformat(), (0, 0))
        series.append({"date": d, "total": n, "failed": f})
    peak = max((s["total"] for s in series), default=0) or 1

    top_users = (
        db.query(
            RequestLog.username, func.count(RequestLog.id), func.sum(RequestLog.cost_usd), func.max(RequestLog.user_id)
        )
        .filter(RequestLog.created_at >= now - timedelta(days=7), RequestLog.username.isnot(None))
        .group_by(RequestLog.username)
        .order_by(func.count(RequestLog.id).desc())
        .limit(5)
        .all()
    )
    recent_errors = (
        db.query(RequestLog).filter(RequestLog.status != "ok").order_by(RequestLog.id.desc()).limit(8).all()
    )
    counts = {
        "users": db.query(User).count(),
        "active_tokens": sum(1 for t in db.query(ApiToken).all() if t.status() == "active"),
    }
    return render(
        request,
        "overview.html",
        admin,
        nav="overview",
        day=day,
        week=week,
        series=series,
        peak=peak,
        top_users=top_users,
        recent_errors=recent_errors,
        counts=counts,
        gate=gate,
    )


# --- users -------------------------------------------------------------------


@router.get("/users")
def users_list(request: Request, q: str = "", admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    query = db.query(User)
    if q:
        like = f"%{q}%"
        query = query.filter((User.username.ilike(like)) | (User.email.ilike(like)))
    users = query.order_by(User.username).all()
    usage = dict(
        db.query(RequestLog.user_id, func.count(RequestLog.id))
        .filter(RequestLog.created_at >= utcnow() - timedelta(days=30))
        .group_by(RequestLog.user_id)
        .all()
    )
    return render(request, "users.html", admin, nav="users", users=users, q=q, usage=usage)


@post("/users")
async def users_create(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    is_admin = form.get("is_admin") == "on"
    password = str(form.get("password", ""))
    if not username:
        flash(request, "Username is required.", "error")
        return back("/admin/users")
    if db.query(User).filter(User.username == username).count():
        flash(request, f"User '{username}' already exists.", "error")
        return back("/admin/users")
    if is_admin and len(password) < 8:
        flash(request, "Admins need a password of at least 8 characters.", "error")
        return back("/admin/users")
    user = User(
        username=username,
        email=str(form.get("email", "")).strip() or None,
        is_admin=is_admin,
        notes=str(form.get("notes", "")).strip() or None,
        password_hash=hash_password(password) if password else None,
    )
    db.add(user)
    db.commit()
    flash(request, f"User '{username}' created.")
    return back(f"/admin/users/{user.id}")


@router.get("/users/{user_id}")
def user_detail(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    since = utcnow() - timedelta(days=30)
    total, ok, cost, tin, tout = (
        db.query(
            func.count(RequestLog.id),
            func.sum(RequestLog.status == "ok"),
            func.sum(RequestLog.cost_usd),
            func.sum(RequestLog.input_tokens),
            func.sum(RequestLog.output_tokens),
        )
        .filter(RequestLog.user_id == user.id, RequestLog.created_at >= since)
        .one()
    )
    stats = {"total": total or 0, "ok": ok or 0, "cost": cost or 0.0, "input_tokens": tin or 0, "output_tokens": tout or 0}
    recent = db.query(RequestLog).filter(RequestLog.user_id == user.id).order_by(RequestLog.id.desc()).limit(10).all()
    enabled_models = runtime_settings.get(db, "enabled_models")
    session_count = db.query(ChatSession).filter(ChatSession.user_id == user.id).count()
    return render(
        request,
        "user_detail.html",
        admin,
        nav="users",
        session_count=session_count,
        user=user,
        stats=stats,
        recent=recent,
        enabled_models=enabled_models,
        now=utcnow(),
    )


@post("/users/{user_id}")
async def user_update(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    if not username:
        flash(request, "Username is required.", "error")
        return back(f"/admin/users/{user.id}")
    clash = db.query(User).filter(User.username == username, User.id != user.id).count()
    if clash:
        flash(request, f"Username '{username}' is taken.", "error")
        return back(f"/admin/users/{user.id}")
    make_admin = form.get("is_admin") == "on"
    if user.is_admin and not make_admin and user.id == admin.id:
        flash(request, "You cannot remove your own admin role.", "error")
        return back(f"/admin/users/{user.id}")
    if make_admin and not user.password_hash:
        flash(request, "Set a password before granting admin.", "error")
        return back(f"/admin/users/{user.id}")
    user.username = username
    user.email = str(form.get("email", "")).strip() or None
    user.notes = str(form.get("notes", "")).strip() or None
    user.is_admin = make_admin
    db.commit()
    flash(request, "User updated.")
    return back(f"/admin/users/{user.id}")


@post("/users/{user_id}/toggle")
def user_toggle(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    if user.id == admin.id:
        flash(request, "You cannot disable yourself.", "error")
    else:
        user.is_active = not user.is_active
        db.commit()
        flash(request, f"User {'enabled' if user.is_active else 'disabled'}.")
    return back(f"/admin/users/{user.id}")


@post("/users/{user_id}/password")
async def user_password(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    form = await request.form()
    password = str(form.get("password", ""))
    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
    else:
        user.password_hash = hash_password(password)
        db.commit()
        flash(request, "Password updated.")
    return back(f"/admin/users/{user.id}")


@post("/users/{user_id}/delete")
def user_delete(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    if user.id == admin.id:
        flash(request, "You cannot delete yourself.", "error")
        return back(f"/admin/users/{user.id}")
    delete_user_transcripts(db, user.id)
    db.delete(user)
    db.commit()
    flash(request, f"User '{user.username}', their tokens and sessions deleted.")
    return back("/admin/users")


# --- tokens ------------------------------------------------------------------


@router.get("/tokens")
def tokens_list(
    request: Request,
    user_id: int | None = None,
    status: str = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(ApiToken).join(User)
    if user_id:
        query = query.filter(ApiToken.user_id == user_id)
    now = utcnow()
    tokens = [t for t in query.order_by(ApiToken.id.desc()).all() if not status or t.status(now) == status]
    users = db.query(User).order_by(User.username).all()
    enabled_models = runtime_settings.get(db, "enabled_models")
    return render(
        request,
        "tokens.html",
        admin,
        nav="tokens",
        tokens=tokens,
        users=users,
        user_id=user_id,
        status=status,
        enabled_models=enabled_models,
        now=now,
    )


def _apply_token_form(token: ApiToken, form) -> None:
    token.name = str(form.get("name", "")).strip() or "default"
    token.expires_at = _end_of_day(form.get("expires_at"))
    token.allowed_models = [str(m) for m in form.getlist("allowed_models")]
    token.rate_limit_rpm = _opt_int(form.get("rate_limit_rpm"))
    max_effort = str(form.get("max_effort", "")).strip()
    token.max_effort = max_effort if max_effort in EFFORTS else None


@post("/tokens")
async def tokens_create(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    user = db.get(User, _opt_int(form.get("user_id")) or 0)
    if user is None:
        flash(request, "Choose a user for the token.", "error")
        return back("/admin/tokens")
    raw = generate_token()
    token = ApiToken(user_id=user.id, prefix=display_prefix(raw), token_hash=hash_token(raw))
    try:
        _apply_token_form(token, form)
    except ValueError as e:
        flash(request, f"Invalid value: {e}", "error")
        return back("/admin/tokens")
    db.add(token)
    db.commit()
    # Rendered directly (never redirected or stored) so the plaintext is shown exactly once.
    return render(request, "token_created.html", admin, nav="tokens", token=token, raw=raw, user=user)


@router.get("/tokens/{token_id}")
def token_detail(request: Request, token_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    token = _get_or_404(db, ApiToken, token_id)
    enabled_models = runtime_settings.get(db, "enabled_models")
    recent = db.query(RequestLog).filter(RequestLog.token_id == token.id).order_by(RequestLog.id.desc()).limit(10).all()
    return render(
        request,
        "token_detail.html",
        admin,
        nav="tokens",
        token=token,
        enabled_models=enabled_models,
        recent=recent,
        now=utcnow(),
    )


@post("/tokens/{token_id}")
async def token_update(request: Request, token_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    token = _get_or_404(db, ApiToken, token_id)
    form = await request.form()
    try:
        _apply_token_form(token, form)
    except ValueError as e:
        flash(request, f"Invalid value: {e}", "error")
        return back(f"/admin/tokens/{token.id}")
    db.commit()
    flash(request, "Token updated.")
    return back(f"/admin/tokens/{token.id}")


@post("/tokens/{token_id}/revoke")
async def token_revoke(request: Request, token_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    token = _get_or_404(db, ApiToken, token_id)
    token.revoked_at = None if token.revoked_at else utcnow()
    db.commit()
    flash(request, "Token restored." if token.revoked_at is None else "Token revoked.")
    form = await request.form()
    return back(_safe_next(form.get("next")) if form.get("next") else f"/admin/tokens/{token.id}")


@post("/tokens/{token_id}/delete")
def token_delete(request: Request, token_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    token = _get_or_404(db, ApiToken, token_id)
    db.delete(token)
    db.commit()
    flash(request, f"Token '{token.name}' deleted.")
    return back("/admin/tokens")


# --- logs --------------------------------------------------------------------

_LOG_FILTERS = ("user_id", "token_id", "model", "status", "date_from", "date_to")


def _filtered_logs(db: Session, params: dict):
    q = db.query(RequestLog)
    if params.get("user_id"):
        q = q.filter(RequestLog.user_id == int(params["user_id"]))
    if params.get("token_id"):
        q = q.filter(RequestLog.token_id == int(params["token_id"]))
    if params.get("model"):
        q = q.filter(RequestLog.model == params["model"])
    if params.get("status"):
        q = q.filter(RequestLog.status == params["status"])
    if params.get("date_from"):
        q = q.filter(RequestLog.created_at >= datetime.combine(date.fromisoformat(params["date_from"]), dtime.min))
    if params.get("date_to"):
        q = q.filter(RequestLog.created_at <= datetime.combine(date.fromisoformat(params["date_to"]), dtime.max))
    return q


def _log_params(request: Request) -> dict:
    return {k: request.query_params.get(k, "").strip() for k in _LOG_FILTERS}


@router.get("/logs")
def logs_list(request: Request, page: int = 1, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    params = _log_params(request)
    try:
        q = _filtered_logs(db, params)
        total = q.count()
    except ValueError:
        flash(request, "Invalid filter value.", "error")
        return back("/admin/logs")
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), pages)
    logs = q.order_by(RequestLog.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    models = [m for (m,) in db.query(RequestLog.model).distinct().all() if m]
    users = db.query(User).order_by(User.username).all()
    query_string = urlencode({k: v for k, v in params.items() if v})
    return render(
        request,
        "logs.html",
        admin,
        nav="logs",
        logs=logs,
        params=params,
        page=page,
        pages=pages,
        total=total,
        models=models,
        users=users,
        query_string=query_string,
        retention=runtime_settings.get(db, "log_retention_days"),
    )


@router.get("/logs/export.csv")
def logs_export(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    q = _filtered_logs(db, _log_params(request)).order_by(RequestLog.id.desc())
    cols = [
        "id", "created_at", "username", "user_id", "token_id", "token_prefix", "model", "effort", "status",
        "http_status", "latency_ms", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "session_id", "client_ip", "error",
    ]  # fmt: skip

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for log in q.yield_per(500):
            writer.writerow([getattr(log, c) for c in cols])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
        yield buf.getvalue()

    filename = f"request-logs-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        rows(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/logs/{log_id}")
def log_detail(request: Request, log_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    log = _get_or_404(db, RequestLog, log_id)
    return render(request, "log_detail.html", admin, nav="logs", log=log)


@post("/logs/purge")
async def logs_purge(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    days = _opt_int(form.get("older_than_days"))
    if days is None or days < 0:
        flash(request, "Enter a number of days (0 deletes everything).", "error")
        return back("/admin/logs")
    cutoff = utcnow() - timedelta(days=days)
    n = db.query(RequestLog).filter(RequestLog.created_at < cutoff).delete()
    db.commit()
    flash(request, f"Deleted {n} log entries.")
    return back("/admin/logs")


# --- settings ----------------------------------------------------------------


@router.get("/settings")
def settings_form(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "settings.html", admin, nav="settings", s=runtime_settings.get_all(db))


@post("/settings")
async def settings_save(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        models = [m.strip() for m in str(form.get("enabled_models", "")).splitlines() if m.strip()]
        models = list(dict.fromkeys(models))
        values = {
            "enabled_models": models,
            "default_model": str(form.get("default_model", "")).strip(),
            "default_effort": str(form.get("default_effort", "")),
            "request_timeout_s": int(form.get("request_timeout_s", 300)),
            "max_concurrency": int(form.get("max_concurrency", 4)),
            "default_rate_limit_rpm": int(form.get("default_rate_limit_rpm", 60)),
            "log_bodies": form.get("log_bodies") == "on",
            "log_retention_days": int(form.get("log_retention_days", 30)),
            "session_retention_days": int(form.get("session_retention_days", 0)),
            "default_system_prompt": str(form.get("default_system_prompt", "")).strip()
            or runtime_settings.DEFAULTS["default_system_prompt"],
            "max_budget_usd_per_request": _opt_float(form.get("max_budget_usd_per_request")),
        }
    except ValueError as e:
        flash(request, f"Invalid value: {e}", "error")
        return back("/admin/settings")
    errors = []
    if not models:
        errors.append("Enable at least one model.")
    if values["default_model"] not in models:
        errors.append("The default model must be one of the enabled models.")
    if values["default_effort"] not in EFFORTS:
        errors.append("Invalid default effort.")
    if values["request_timeout_s"] < 5 or values["max_concurrency"] < 1:
        errors.append("Timeout must be ≥ 5s and concurrency ≥ 1.")
    if min(values["default_rate_limit_rpm"], values["log_retention_days"], values["session_retention_days"]) < 0:
        errors.append("Rate limit and retention cannot be negative.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return back("/admin/settings")
    runtime_settings.set_many(db, values)
    gate.resize(values["max_concurrency"])
    flash(request, "Settings saved.")
    return back("/admin/settings")


# --- chat --------------------------------------------------------------------


def _chat_sidebar(db: Session, admin: User) -> list[ChatSession]:
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == admin.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(100)
        .all()
    )


@router.get("/chat")
def chat_new(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    s = runtime_settings.get_all(db)
    return render(
        request,
        "chat.html",
        admin,
        nav="chat",
        session=None,
        sessions=_chat_sidebar(db, admin),
        enabled_models=s["enabled_models"],
        model=s["default_model"],
        effort=s["default_effort"],
        default_system_prompt=s["default_system_prompt"],
    )


@router.get("/chat/{session_id}")
def chat_view(request: Request, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_or_404(db, ChatSession, session_id)
    if session.user_id != admin.id:
        return back(f"/admin/sessions/{session.id}")  # other users' conversations are read-only
    s = runtime_settings.get_all(db)
    return render(
        request,
        "chat.html",
        admin,
        nav="chat",
        session=session,
        sessions=_chat_sidebar(db, admin),
        enabled_models=s["enabled_models"],
        model=session.model,
        effort=session.effort,
        default_system_prompt=s["default_system_prompt"],
    )


def _chat_choice(form, settings: dict, model_default: str, effort_default: str) -> tuple[str, str]:
    model = str(form.get("model") or model_default)
    effort = str(form.get("effort") or effort_default)
    if model not in settings["enabled_models"]:
        raise ValueError(f"Model '{model}' is not enabled.")
    if effort not in EFFORTS:
        raise ValueError(f"Invalid effort '{effort}'.")
    return model, effort


@post("/chat/new")
async def chat_create(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    settings = runtime_settings.get_all(db)
    try:
        model, effort = _chat_choice(form, settings, settings["default_model"], settings["default_effort"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    session = create_session(
        db,
        user=admin,
        model=model,
        effort=effort,
        system_prompt=str(form.get("system_prompt") or ""),
        source="dashboard",
    )
    return {"id": session.id}


@post("/chat/{session_id}/send")
async def chat_send(request: Request, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != admin.id:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)
    form = await request.form()
    settings = runtime_settings.get_all(db)
    content = str(form.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "Type a message first."}, status_code=400)
    try:
        model, effort = _chat_choice(form, settings, session.model, session.effort)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    started = time.monotonic()
    log = RequestLog(
        endpoint="dashboard.chat", user_id=admin.id, username=admin.username, model=model, effort=effort,
        chat_session_id=session.id, client_ip=request.client.host if request.client else None,
        status="error", http_status=500, trace_id=telemetry.current_trace_id(),
    )  # fmt: skip
    try:
        gate.resize(int(settings["max_concurrency"]))
        result = await run_turn(db, session, content, model=model, effort=effort, settings=settings)
        record_result(log, result, settings, prompt=content)
        return {
            "title": session.title,
            "reply": result.text,
            "meta": {
                "model": model,
                "effort": effort,
                "input_tokens": result.usage.get("input_tokens"),
                "output_tokens": result.usage.get("output_tokens"),
                "cost_usd": result.cost_usd,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        }
    except ClaudeError as e:
        log.status, log.http_status, log.error = "error", e.status, e.message
        discarded = session.message_count == 0
        if discarded:  # a conversation whose first turn failed has nothing worth keeping
            delete_session(db, session)
            log.chat_session_id = None
        return JSONResponse({"error": e.message, "discarded": discarded}, status_code=e.status)
    finally:
        log.latency_ms = int((time.monotonic() - started) * 1000)
        db.add(log)
        db.commit()
        telemetry.record_request(
            endpoint=log.endpoint, status=log.status, http_status=log.http_status,
            code=None if log.status == "ok" else "upstream_error", model=log.model, effort=log.effort,
            latency_ms=log.latency_ms, username=log.username, token_prefix=None, chat_session_id=log.chat_session_id,
        )  # fmt: skip


@post("/chat/{session_id}/rename")
async def chat_rename(request: Request, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_or_404(db, ChatSession, session_id)
    form = await request.form()
    title = " ".join(str(form.get("title") or "").split())[:80]
    if title:
        session.title = title
        db.commit()
    return back(_safe_next(form.get("next")) if form.get("next") else f"/admin/sessions/{session.id}")


# --- sessions ----------------------------------------------------------------


@router.get("/sessions")
def sessions_list(
    request: Request,
    user_id: int | None = None,
    source: str = "",
    q: str = "",
    page: int = 1,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(ChatSession).join(User)
    if user_id:
        query = query.filter(ChatSession.user_id == user_id)
    if source:
        query = query.filter(ChatSession.source == source)
    if q:
        query = query.filter(ChatSession.title.ilike(f"%{q}%"))
    total = query.count()
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), pages)
    sessions = query.order_by(ChatSession.updated_at.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    params = {k: v for k, v in {"user_id": user_id or "", "source": source, "q": q}.items() if v}
    return render(
        request,
        "sessions.html",
        admin,
        nav="sessions",
        sessions=sessions,
        users=db.query(User).order_by(User.username).all(),
        user_id=user_id,
        source=source,
        q=q,
        page=page,
        pages=pages,
        total=total,
        query_string=urlencode(params),
        retention=runtime_settings.get(db, "session_retention_days"),
    )


@router.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_or_404(db, ChatSession, session_id)
    return render(request, "session_detail.html", admin, nav="sessions", session=session)


@post("/sessions/{session_id}/delete")
async def session_delete(request: Request, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_or_404(db, ChatSession, session_id)
    form = await request.form()
    delete_session(db, session)
    flash(request, "Conversation deleted.")
    return back(_safe_next(form.get("next")) if form.get("next") else "/admin/sessions")


# --- system ------------------------------------------------------------------


def _mask(secret: str | None) -> str:
    if not secret:
        return "not set"
    return f"{secret[:10]}…{secret[-4:]}" if len(secret) > 16 else "set"


async def _system_ctx(db: Session) -> dict:
    settings = get_settings()
    return {
        "version": await claude_version(),
        "oauth": _mask(settings.claude_code_oauth_token),
        "api_key": _mask(os.environ.get("ANTHROPIC_API_KEY")),
        "claude_bin": settings.claude_bin,
        "database_url": settings.database_url,
        "gate": gate,
        "enabled_models": runtime_settings.get(db, "enabled_models"),
    }


@router.get("/system")
async def system(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "system.html", admin, nav="system", test=None, **(await _system_ctx(db)))


@post("/system/test")
async def system_test(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    model = str(form.get("model") or "haiku")
    effort = str(form.get("effort") or "low")
    prompt = str(form.get("prompt") or "Reply with exactly: pong")
    started = time.monotonic()
    test = {"model": model, "effort": effort, "prompt": prompt}
    system_prompt = runtime_settings.get(db, "default_system_prompt")
    timeout_s = float(runtime_settings.get(db, "request_timeout_s"))
    release_connection(db)
    try:
        result = await run_claude(
            prompt,
            model=model,
            effort=effort if effort in EFFORTS else "low",
            system_prompt=system_prompt,
            timeout_s=timeout_s,
        )
        test.update(ok=True, text=result.text, cost=result.cost_usd, usage=result.usage)
    except ClaudeError as e:
        test.update(ok=False, text=e.message)
    test["latency_ms"] = int((time.monotonic() - started) * 1000)
    return render(request, "system.html", admin, nav="system", test=test, **(await _system_ctx(db)))
