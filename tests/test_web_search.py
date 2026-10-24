import json

from app import runtime_settings
from app.claude_runner import WEB_SEARCH_MAX_TURNS
from app.models import ChatMessage, ChatSession, RequestLog
from tests.conftest import client_csrf

ECHO = "echo[haiku|low]: hi"
SEARCH = {"web_search_options": {}}


def H(raw):
    return {"Authorization": f"Bearer {raw}"}


def chat(client, raw, **body):
    body.setdefault("model", "haiku")
    body.setdefault("effort", "low")
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return client.post("/v1/chat/completions", json=body, headers=H(raw))


def streamed_text(r) -> str:
    chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    return "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c.get("choices"))


def argv_of(path) -> list[str]:
    return json.loads(path.read_text())["argv"]


def test_off_by_default(client, make_token, db, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    r = chat(client, raw, **SEARCH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "web_search_disabled"
    assert not args_file.exists()  # rejected before Claude ran
    log = db.query(RequestLog).one()
    assert log.status == "rejected" and log.http_status == 400

    sid = client.post("/v1/sessions", json={"model": "haiku"}, headers=H(raw)).json()["id"]
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "hi", **SEARCH}, headers=H(raw))
    assert r.status_code == 400 and r.json()["error"]["code"] == "web_search_disabled"


def test_opt_in_per_request(client, make_token, db, tmp_path, monkeypatch):
    runtime_settings.set_many(db, {"web_search_enabled": True})
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()

    # Enabled on the server, but this request didn't ask: still no tools.
    assert chat(client, raw).status_code == 200
    argv = argv_of(args_file)
    assert argv[argv.index("--tools") + 1] == "" and "--allowedTools" not in argv

    r = chat(client, raw, web_search_options={"search_context_size": "low", "user_location": {"type": "approximate"}})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == ECHO
    argv = argv_of(args_file)
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--max-turns") + 1] == str(WEB_SEARCH_MAX_TURNS)
    assert "WebFetch" not in " ".join(argv)
    logs = db.query(RequestLog).order_by(RequestLog.id).all()
    assert [log.web_searches for log in logs] == [None, 1]


def test_stream_keeps_text_from_every_turn(client, make_token, db):
    runtime_settings.set_many(db, {"web_search_enabled": True})
    raw, _ = make_token()
    r = chat(client, raw, stream=True, **SEARCH)
    assert r.status_code == 200
    assert streamed_text(r) == "Searching the web.\n\n" + ECHO
    assert r.text.rstrip().endswith("data: [DONE]")
    assert db.query(RequestLog).one().web_searches == 1


def test_session_stores_streamed_text_and_rebuilds_without_search(client, make_token, db, tmp_path, monkeypatch):
    runtime_settings.set_many(db, {"web_search_enabled": True})
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    sid = client.post("/v1/sessions", json={"model": "haiku", "effort": "low"}, headers=H(raw)).json()["id"]
    url = f"/v1/sessions/{sid}/messages"

    r = client.post(url, json={"content": "news?", "stream": True, **SEARCH}, headers=H(raw))
    shown = streamed_text(r)
    assert shown.startswith("Searching the web.\n\n")
    stored = db.query(ChatMessage).filter_by(session_id=sid, role="assistant").one()
    assert stored.content == shown and stored.web_searches == 1
    first_claude_id = db.get(ChatSession, sid).claude_session_id

    # The transcript holds a search, so a turn without the tool can't resume it (the fake fails like the
    # API would): the conversation is rebuilt from the stored messages instead.
    r = client.post(url, json={"content": "thanks"}, headers=H(raw))
    assert r.status_code == 200, r.text
    argv = argv_of(args_file)
    assert "--session-id" in argv and "--resume" not in argv
    assert "news?" in json.loads(args_file.read_text())["stdin"]
    db.expire_all()
    session = db.get(ChatSession, sid)
    assert session.claude_session_id != first_claude_id and session.transcript_web_search is False

    # The rebuilt transcript has no search in it, so the next plain turn resumes it.
    assert client.post(url, json={"content": "bye"}, headers=H(raw)).status_code == 200
    assert "--resume" in argv_of(args_file)


def test_search_turns_resume_a_search_transcript(client, make_token, db, tmp_path, monkeypatch):
    runtime_settings.set_many(db, {"web_search_enabled": True})
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    r = chat(client, raw, session_id="new", **SEARCH)
    sid = r.json()["session_id"]
    r = chat(client, raw, session_id=sid, messages=[{"role": "user", "content": "more"}], **SEARCH)
    assert r.status_code == 200, r.text
    assert "--resume" in argv_of(args_file)
    assert db.get(ChatSession, sid).transcript_web_search is True


def test_dashboard_chat(admin_client, db):
    csrf = client_csrf(admin_client, "/admin/chat")
    assert 'id="web-search"' not in admin_client.get("/admin/chat").text
    sid = admin_client.post("/admin/chat/new", data={"csrf_token": csrf, "model": "haiku", "effort": "low"}).json()["id"]
    send = {"csrf_token": csrf, "content": "hi", "web_search": "1"}
    r = admin_client.post(f"/admin/chat/{sid}/send", data=send)
    assert r.status_code == 400 and "turned off" in r.json()["error"]

    base = {
        "csrf_token": csrf, "enabled_models": "haiku\nsonnet", "default_model": "haiku", "default_effort": "low",
        "request_timeout_s": "60", "max_concurrency": "2", "default_rate_limit_rpm": "10",
        "log_retention_days": "7", "default_system_prompt": "Hi",
    }  # fmt: skip
    admin_client.post("/admin/settings", data={**base, "web_search_enabled": "on"})
    db.expire_all()
    assert runtime_settings.get(db, "web_search_enabled") is True
    assert 'id="web-search"' in admin_client.get(f"/admin/chat/{sid}").text

    r = admin_client.post(f"/admin/chat/{sid}/send", data={**send, "stream": "1"})
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert "".join(e.get("delta", "") for e in events).startswith("Searching the web.\n\n")
    assert events[-1]["done"] and events[-1]["meta"]["web_searches"] == 1
    assert "1 search" in admin_client.get(f"/admin/chat/{sid}").text

    admin_client.post("/admin/settings", data=base)  # unticked checkbox = off
    db.expire_all()
    assert runtime_settings.get(db, "web_search_enabled") is False


def test_response_reports_search_count(client, make_token, db):
    # Callers need to know a search really ran (vs. an answer from training data): usage carries the count.
    runtime_settings.set_many(db, {"web_search_enabled": True})
    raw, _ = make_token()
    usage = chat(client, raw, **SEARCH).json()["usage"]
    assert usage["server_tool_use"] == {"web_search_requests": 1}
    assert "server_tool_use" not in chat(client, raw).json()["usage"]

    sid = client.post("/v1/sessions", json={"model": "haiku"}, headers=H(raw)).json()["id"]
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "hi", **SEARCH}, headers=H(raw))
    assert r.json()["usage"]["server_tool_use"] == {"web_search_requests": 1}
