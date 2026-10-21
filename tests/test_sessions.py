import json
from datetime import timedelta

from sqlalchemy import create_engine, inspect, text

from app import runtime_settings
from app.claude_runner import transcript_paths
from app.models import ChatSession, RequestLog, utcnow
from tests.conftest import client_csrf


def H(raw):
    return {"Authorization": f"Bearer {raw}"}


def last_argv(path):
    return json.loads(path.read_text())


def test_session_api_resumes_claude_session(client, make_token, db, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()

    r = client.post("/v1/sessions", json={"model": "haiku", "effort": "low", "system_prompt": "Be brief."}, headers=H(raw))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r1 = client.post(f"/v1/sessions/{sid}/messages", json={"content": "first"}, headers=H(raw))
    assert r1.status_code == 200, r1.text
    assert r1.json()["choices"][0]["message"]["content"] == "echo[haiku|low]#1: first"
    assert r1.json()["session_id"] == sid
    seen = last_argv(args_file)
    assert "--session-id" in seen["argv"] and "--no-session-persistence" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--system-prompt") + 1] == "Be brief."
    claude_id = seen["argv"][seen["argv"].index("--session-id") + 1]

    r2 = client.post(f"/v1/sessions/{sid}/messages", json={"content": "second", "effort": "high"}, headers=H(raw))
    assert r2.json()["choices"][0]["message"]["content"] == "echo[haiku|high]#2: second"
    seen = last_argv(args_file)
    assert seen["argv"][seen["argv"].index("--resume") + 1] == claude_id
    assert seen["stdin"] == "second"  # only the new message; history lives in Claude's transcript

    got = client.get(f"/v1/sessions/{sid}", headers=H(raw)).json()
    assert [m["role"] for m in got["messages"]] == ["user", "assistant", "user", "assistant"]
    assert got["title"] == "first" and got["message_count"] == 4 and got["effort"] == "high"
    assert [s["id"] for s in client.get("/v1/sessions", headers=H(raw)).json()["data"]] == [sid]
    assert db.query(RequestLog).filter_by(chat_session_id=sid, status="ok").count() == 2

    assert transcript_paths(claude_id)
    assert client.delete(f"/v1/sessions/{sid}", headers=H(raw)).json()["deleted"] is True
    assert not transcript_paths(claude_id)
    assert client.get(f"/v1/sessions/{sid}", headers=H(raw)).status_code == 404


def test_sessions_are_private_to_their_user(client, make_token):
    raw_a, _ = make_token("alice")
    raw_b, _ = make_token("bob")
    sid = client.post("/v1/sessions", json={}, headers=H(raw_a)).json()["id"]
    assert client.get(f"/v1/sessions/{sid}", headers=H(raw_b)).status_code == 404
    assert client.post(f"/v1/sessions/{sid}/messages", json={"content": "x"}, headers=H(raw_b)).status_code == 404
    assert client.delete(f"/v1/sessions/{sid}", headers=H(raw_b)).status_code == 404
    assert client.get("/v1/sessions", headers=H(raw_b)).json()["data"] == []


def test_session_respects_token_limits(client, make_token):
    raw, _ = make_token(allowed_models=["haiku"], max_effort="low")
    assert client.post("/v1/sessions", json={"model": "opus"}, headers=H(raw)).status_code == 403
    sid = client.post("/v1/sessions", json={"model": "haiku", "effort": "low"}, headers=H(raw)).json()["id"]
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "x", "effort": "max"}, headers=H(raw))
    assert r.json()["error"]["code"] == "effort_not_allowed"
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "  "}, headers=H(raw))
    assert r.status_code == 400


def test_chat_completions_session_extension(client, make_token, db):
    raw, _ = make_token()
    body = {"model": "haiku", "session_id": "new", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "one"}]}
    r = client.post("/v1/chat/completions", json=body, headers=H(raw))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert db.get(ChatSession, sid).system_prompt == "S"

    body = {"model": "haiku", "session_id": sid, "messages": [{"role": "user", "content": "two"}]}
    r = client.post("/v1/chat/completions", json=body, headers=H(raw))
    assert r.json()["choices"][0]["message"]["content"] == "echo[haiku|medium]#2: two"

    body["session_id"] = "does-not-exist"
    assert client.post("/v1/chat/completions", json=body, headers=H(raw)).status_code == 404


def test_failed_new_session_is_not_kept(client, make_token, db, monkeypatch):
    raw, _ = make_token()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    body = {"session_id": "new", "messages": [{"role": "user", "content": "x"}]}
    assert client.post("/v1/chat/completions", json=body, headers=H(raw)).status_code == 502
    assert db.query(ChatSession).count() == 0


def test_lost_transcript_is_rebuilt_from_history(client, make_token, db, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    sid = client.post("/v1/sessions", json={}, headers=H(raw)).json()["id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"content": "remember 42"}, headers=H(raw))
    old = db.get(ChatSession, sid).claude_session_id
    for p in transcript_paths(old):
        p.unlink()

    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "what number?"}, headers=H(raw))
    assert r.status_code == 200
    seen = last_argv(args_file)
    assert "--session-id" in seen["argv"] and "--resume" not in seen["argv"]
    assert "<user>\nremember 42\n</user>" in seen["stdin"] and seen["stdin"].endswith("what number?")
    db.expire_all()
    assert db.get(ChatSession, sid).claude_session_id != old


def test_session_retention(client, make_token, db):
    from app.main import purge_expired

    raw, _ = make_token()
    sid = client.post("/v1/sessions", json={}, headers=H(raw)).json()["id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"content": "x"}, headers=H(raw))
    session = db.get(ChatSession, sid)
    claude_id = session.claude_session_id
    session.updated_at = utcnow() - timedelta(days=10)
    db.commit()
    assert purge_expired()[1] == 0  # retention 0 = keep forever
    runtime_settings.set_many(db, {"session_retention_days": 5})
    assert purge_expired()[1] == 1
    assert not transcript_paths(claude_id)


def test_dashboard_chat(admin_client, db):
    csrf = client_csrf(admin_client, "/admin/chat")
    r = admin_client.post("/admin/chat/new", data={"csrf_token": csrf, "model": "haiku", "effort": "low", "system_prompt": ""})
    sid = r.json()["id"]
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "hello", "model": "haiku", "effort": "low"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "echo[haiku|low]#1: hello" and r.json()["title"] == "hello"
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "again"})
    assert r.json()["reply"] == "echo[haiku|low]#2: again"
    assert admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": "bad", "content": "x"}).status_code == 403
    assert db.query(RequestLog).filter_by(endpoint="dashboard.chat", status="ok").count() == 2

    for path in ["/admin/chat", f"/admin/chat/{sid}", "/admin/sessions", f"/admin/sessions/{sid}", "/admin/sessions?source=dashboard"]:
        page = admin_client.get(path)
        assert page.status_code == 200, path
    assert "echo[haiku|low]#2: again" in admin_client.get(f"/admin/chat/{sid}").text

    admin_client.post(f"/admin/chat/{sid}/rename", data={"csrf_token": csrf, "title": "Renamed  chat"})
    db.expire_all()
    assert db.get(ChatSession, sid).title == "Renamed chat"
    admin_client.post(f"/admin/sessions/{sid}/delete", data={"csrf_token": csrf})
    db.expire_all()
    assert db.get(ChatSession, sid) is None


def test_dashboard_failed_first_turn_is_discarded(admin_client, db, monkeypatch):
    csrf = client_csrf(admin_client, "/admin/chat")
    sid = admin_client.post("/admin/chat/new", data={"csrf_token": csrf}).json()["id"]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "hi"})
    assert r.status_code == 502 and r.json()["discarded"] is True
    db.expire_all()
    assert db.get(ChatSession, sid) is None


def test_admin_sees_api_sessions_read_only(admin_client, make_token):
    raw, _ = make_token()
    sid = admin_client.post("/v1/sessions", json={}, headers=H(raw)).json()["id"]
    r = admin_client.get(f"/admin/chat/{sid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/sessions/{sid}"


def test_schema_upgrade_adds_new_columns(tmp_path, monkeypatch):
    from app import db as app_db
    from app.main import create_schema

    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE request_logs (id INTEGER PRIMARY KEY, created_at DATETIME, endpoint VARCHAR(64), status VARCHAR(16), http_status INTEGER)"))
    old.dispose()
    app_db.init_engine(url)
    create_schema()
    cols = {c["name"] for c in inspect(app_db.get_engine()).get_columns("request_logs")}
    assert {"chat_session_id", "cost_usd"} <= cols
    assert "chat_sessions" in inspect(app_db.get_engine()).get_table_names()
