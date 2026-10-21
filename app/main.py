import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, runtime_settings, telemetry
from app.config import get_settings
from sqlalchemy import inspect, text

from app.db import Base, get_engine, session_factory
from app.models import RequestLog, User, utcnow
from app.routers import admin, api, docs
from app.security import CSRFError, LoginRequired, hash_password
from app.sessions import purge_stale_sessions

# Uvicorn configures this logger, so messages show up in `docker compose logs`.
log = logging.getLogger("uvicorn.error")


def create_schema() -> None:
    """Create tables, then add any column a newer release introduced (additive changes only)."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing and column.nullable:
                    col_type = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                    log.info("Added column %s.%s", table.name, column.name)


def bootstrap_admin() -> None:
    settings = get_settings()
    with session_factory() as db:
        if db.query(User).filter(User.is_admin.is_(True)).count():
            return
        if not settings.admin_username or not settings.admin_password:
            raise RuntimeError("No admin user exists: set ADMIN_USERNAME and ADMIN_PASSWORD to create one.")
        existing = db.query(User).filter(User.username == settings.admin_username).one_or_none()
        if existing:
            existing.is_admin, existing.is_active = True, True
            existing.password_hash = hash_password(settings.admin_password)
        else:
            db.add(
                User(
                    username=settings.admin_username,
                    is_admin=True,
                    password_hash=hash_password(settings.admin_password),
                )
            )
        db.commit()
        log.info("Created admin user %r", settings.admin_username)


def purge_expired() -> tuple[int, int]:
    """Apply log and session retention. Returns (logs deleted, sessions deleted)."""
    with session_factory() as db:
        logs = 0
        days = int(runtime_settings.get(db, "log_retention_days") or 0)
        if days > 0:
            cutoff = utcnow() - timedelta(days=days)
            logs = db.query(RequestLog).filter(RequestLog.created_at < cutoff).delete()
            db.commit()
        sessions = purge_stale_sessions(db, int(runtime_settings.get(db, "session_retention_days") or 0))
        return logs, sessions


async def _retention_loop() -> None:
    while True:
        try:
            logs, sessions = await asyncio.to_thread(purge_expired)
            if logs or sessions:
                log.info("Retention: purged %d request logs and %d sessions", logs, sessions)
        except Exception:
            log.exception("Retention purge failed")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_schema()
    bootstrap_admin()
    task = asyncio.create_task(_retention_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    telemetry.shutdown()  # flush pending spans, metrics and logs


def create_app() -> FastAPI:
    settings = get_settings()
    if not settings.secret_key or settings.secret_key == "change-me":
        raise RuntimeError("Set SECRET_KEY to a long random string (it signs admin sessions).")
    # FastAPI's generated spec and Swagger UI are off: the hand-written spec in docs/ is served instead,
    # with our own reference page at /docs.
    app = FastAPI(
        title="Claude Headless API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="claude_api_session",
        same_site="strict",
        https_only=settings.session_cookie_secure,
        max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(admin.router)
    app.include_router(docs.router)
    telemetry.setup(app, get_engine())  # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=303)

    @app.exception_handler(CSRFError)
    async def _csrf(_request: Request, _exc: CSRFError):
        return PlainTextResponse("CSRF check failed. Reload the page and try again.", status_code=403)

    static_dir = Path(__file__).parent / "static"

    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon():
        # Browsers ask for /favicon.ico on their own (JSON pages, bookmarks), not only via <link>.
        return FileResponse(static_dir / "favicon.ico", media_type="image/x-icon")

    @app.get("/", include_in_schema=False)
    async def _root():
        return RedirectResponse("/admin", status_code=303)

    @app.exception_handler(404)
    async def _not_found(request: Request, _exc):
        if request.url.path.startswith("/v1/"):
            body = {"error": {"message": "Not found.", "type": "invalid_request_error", "param": None, "code": None}}
            return JSONResponse(body, status_code=404)
        return PlainTextResponse("Not found", status_code=404)

    return app


app = create_app()
