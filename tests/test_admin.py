import re

from app.models import ApiToken, User
from app.security import hash_token
from tests.conftest import client_csrf


def test_requires_login(client):
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")


def test_bad_login(client):
    csrf = client_csrf(client, "/admin/login")
    r = client.post("/admin/login", data={"username": "admin", "password": "wrong", "csrf_token": csrf})
    assert r.status_code == 401


def test_csrf_enforced(admin_client):
    r = admin_client.post("/admin/users", data={"username": "mallory", "csrf_token": "forged"})
    assert r.status_code == 403


def test_open_redirect_blocked(client):
    csrf = client_csrf(client, "/admin/login")
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin-password", "csrf_token": csrf, "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/admin"


def test_user_and_token_flow(admin_client, db):
    csrf = client_csrf(admin_client)
    r = admin_client.post("/admin/users", data={"username": "dave", "email": "d@example.com", "csrf_token": csrf})
    assert r.status_code == 200
    dave = db.query(User).filter_by(username="dave").one()

    r = admin_client.post(
        "/admin/tokens",
        data={
            "csrf_token": csrf,
            "user_id": dave.id,
            "name": "laptop",
            "allowed_models": ["haiku", "sonnet"],
            "rate_limit_rpm": "5",
            "max_effort": "high",
            "expires_at": "2099-01-01",
        },
    )
    assert r.status_code == 200
    raw = re.search(r'id="new-token">(chk_[^<]+)<', r.text).group(1)
    db.expire_all()
    token = db.query(ApiToken).filter_by(user_id=dave.id).one()
    assert token.token_hash == hash_token(raw) and raw not in (token.prefix + token.name)
    assert token.allowed_models == ["haiku", "sonnet"] and token.rate_limit_rpm == 5 and token.max_effort == "high"

    # The token works, and the plaintext is never shown again.
    ok = admin_client.post(
        "/v1/chat/completions",
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert ok.status_code == 200
    assert raw not in admin_client.get(f"/admin/tokens/{token.id}").text

    # Every page renders with data present.
    for path in ["/admin", "/admin/users", f"/admin/users/{dave.id}", "/admin/tokens", "/admin/logs",
                 "/admin/logs/1", "/admin/settings", "/admin/system", "/admin/logs/export.csv"]:  # fmt: skip
        assert admin_client.get(path).status_code == 200, path

    admin_client.post(f"/admin/tokens/{token.id}/revoke", data={"csrf_token": csrf})
    denied = admin_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert denied.status_code == 401

    admin_client.post(f"/admin/users/{dave.id}/delete", data={"csrf_token": csrf})
    db.expire_all()
    assert db.query(User).filter_by(username="dave").count() == 0
    assert db.query(ApiToken).count() == 0


def test_cannot_disable_or_delete_self(admin_client, db):
    csrf = client_csrf(admin_client)
    me = db.query(User).filter_by(username="admin").one()
    admin_client.post(f"/admin/users/{me.id}/toggle", data={"csrf_token": csrf})
    admin_client.post(f"/admin/users/{me.id}/delete", data={"csrf_token": csrf})
    db.expire_all()
    assert db.get(User, me.id).is_active


def test_settings_validation_and_save(admin_client, db):
    from app import runtime_settings

    csrf = client_csrf(admin_client)
    base = {
        "csrf_token": csrf, "enabled_models": "haiku\nsonnet", "default_model": "opus", "default_effort": "low",
        "request_timeout_s": "60", "max_concurrency": "2", "default_rate_limit_rpm": "10",
        "log_retention_days": "7", "default_system_prompt": "Hi",
    }  # fmt: skip
    admin_client.post("/admin/settings", data=base)
    assert runtime_settings.get(db, "default_model") == "sonnet"  # rejected: opus not enabled
    admin_client.post("/admin/settings", data={**base, "default_model": "haiku", "log_bodies": "on"})
    db.expire_all()
    s = runtime_settings.get_all(db)
    assert s["enabled_models"] == ["haiku", "sonnet"] and s["default_model"] == "haiku" and s["log_bodies"] is True


def test_system_test_button(admin_client):
    csrf = client_csrf(admin_client, "/admin/system")
    r = admin_client.post("/admin/system/test", data={"csrf_token": csrf, "model": "haiku", "effort": "low", "prompt": "ping"})
    assert "echo[haiku|low]: ping" in r.text
    assert "9.9.9 (Fake Claude)" in r.text


def test_favicon(client):
    assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"
    assert client.get("/static/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    for page in ("/admin/login", "/docs"):
        assert 'rel="icon" href="/static/favicon.svg"' in client.get(page).text
