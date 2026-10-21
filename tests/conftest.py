import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent

# Must be set before the app (and its cached Settings) is imported.
os.environ["CLAUDE_BIN"] = str(HERE / "fake_claude.py")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-test-token-value"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Real env vars beat values from a developer's .env, so these keep tests independent of it.
os.environ["SESSION_COOKIE_SECURE"] = "false"
sys.path.insert(0, str(HERE.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db as app_db  # noqa: E402
from app.models import ApiToken, User  # noqa: E402
from app.ratelimit import limiter  # noqa: E402
from app.security import display_prefix, generate_token, hash_token  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    # The runner forwards only allow-listed env vars; let the fake CLI's switches through.
    from app import claude_runner

    passthrough = claude_runner._ENV_PASSTHROUGH + ("FAKE_CLAUDE_MODE", "FAKE_CLAUDE_ARGS_FILE")
    monkeypatch.setattr(claude_runner, "_ENV_PASSTHROUGH", passthrough)
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "claude_data_dir", str(tmp_path / "claude"))
    app_db.init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    limiter.reset()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db(client):
    s = app_db.session_factory()
    yield s
    s.close()


@pytest.fixture
def make_token(db):
    def _make(username="alice", **token_kwargs):
        user = db.query(User).filter_by(username=username).one_or_none()
        if user is None:
            user = User(username=username)
            db.add(user)
            db.commit()
        raw = generate_token()
        token = ApiToken(
            user_id=user.id, name="t", prefix=display_prefix(raw), token_hash=hash_token(raw), **token_kwargs
        )
        db.add(token)
        db.commit()
        return raw, token

    return _make


@pytest.fixture
def admin_client(client):
    client.get("/admin/login")
    csrf = client_csrf(client, "/admin/login")
    r = client.post("/admin/login", data={"username": "admin", "password": "admin-password", "csrf_token": csrf})
    assert r.status_code in (200, 303), r.text
    return client


def client_csrf(client, path="/admin"):
    import re

    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found"
    return m.group(1)
