import json
from datetime import timedelta

from app import runtime_settings
from app.models import RequestLog, User, utcnow


def chat(client, token, **body):
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"})


def test_happy_path_shape_and_log(client, make_token, db):
    raw, token = make_token()
    r = chat(client, raw, model="haiku", effort="high")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "haiku"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "echo[haiku|high]: hi"}
    assert body["usage"] == {
        "prompt_tokens": 113,
        "completion_tokens": 5,
        "total_tokens": 118,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    log = db.query(RequestLog).one()
    assert (log.status, log.model, log.effort, log.user_id, log.cost_usd) == ("ok", "haiku", "high", token.user_id, 0.0123)
    assert log.prompt is None and log.response is None  # bodies off by default


def test_defaults_and_x_api_key(client, make_token):
    raw, _ = make_token()
    r = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "yo"}]}, headers={"x-api-key": raw}
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "echo[sonnet|medium]: yo"


def test_reasoning_effort_mapping(client, make_token):
    raw, _ = make_token()
    assert "|high]" in chat(client, raw, reasoning_effort="high").json()["choices"][0]["message"]["content"]
    assert "|low]" in chat(client, raw, reasoning_effort="minimal").json()["choices"][0]["message"]["content"]
    assert chat(client, raw, reasoning_effort="bogus").status_code == 400
    assert chat(client, raw, effort="bogus").status_code == 400


def test_argv_and_isolation(client, make_token, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("SOME_SERVER_SECRET", "x")
    raw, _ = make_token()
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": [{"type": "text", "text": "Q2"}]},
    ]
    assert chat(client, raw, messages=messages).status_code == 200
    seen = json.loads(args_file.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--system-prompt") + 1] == "Be terse."
    assert argv[argv.index("--tools") + 1] == ""
    assert "-p" in argv and "--no-session-persistence" in argv
    assert "<user>\nQ1\n</user>" in seen["stdin"] and "<assistant>\nA1\n</assistant>" in seen["stdin"]
    assert seen["stdin"].endswith("Q2")
    assert "SOME_SERVER_SECRET" not in seen["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in seen["env"]
    assert "claude-req-" in seen["cwd"]


def test_json_schema_passthrough(client, make_token, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}
    assert chat(client, raw, response_format=rf).status_code == 200
    argv = json.loads(args_file.read_text())["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_auth_failures(client, make_token, db):
    assert chat(client, "").status_code == 401
    assert chat(client, "chk_nope").json()["error"]["code"] == "invalid_api_key"

    raw, token = make_token(revoked_at=utcnow())
    assert chat(client, raw).json()["error"]["code"] == "api_key_revoked"

    raw, token = make_token(username="bob", expires_at=utcnow() - timedelta(seconds=1))
    assert chat(client, raw).json()["error"]["code"] == "api_key_expired"

    raw, token = make_token(username="carol")
    db.get(User, token.user_id).is_active = False
    db.commit()
    assert chat(client, raw).status_code == 403
    assert db.query(RequestLog).filter_by(status="rejected").count() == 5


def test_model_and_effort_restrictions(client, make_token):
    raw, _ = make_token(allowed_models=["haiku"], max_effort="medium")
    assert chat(client, raw, model="gpt-4").json()["error"]["code"] == "model_not_found"
    assert chat(client, raw, model="opus").json()["error"]["code"] == "model_not_allowed"
    assert chat(client, raw, model="haiku", effort="max").json()["error"]["code"] == "effort_not_allowed"
    assert chat(client, raw, model="haiku", effort="medium").status_code == 200
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).json()
    assert [m["id"] for m in models["data"]] == ["haiku"]


def test_rate_limit(client, make_token):
    raw, _ = make_token(rate_limit_rpm=2)
    assert chat(client, raw).status_code == 200
    assert chat(client, raw).status_code == 200
    r = chat(client, raw)
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"


def test_invalid_requests(client, make_token):
    raw, _ = make_token()
    assert chat(client, raw, stream=True).json()["error"]["code"] == "stream_unsupported"
    img = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    assert chat(client, raw, messages=img).status_code == 400
    assert chat(client, raw, messages=[{"role": "assistant", "content": "x"}]).status_code == 400
    assert chat(client, raw, messages=[]).status_code == 400
    r = client.post("/v1/chat/completions", content=b"{nope", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 400


def test_upstream_errors(client, make_token, monkeypatch, db):
    raw, _ = make_token()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    r = chat(client, raw)
    assert r.status_code == 502 and "Failed to authenticate" in r.json()["error"]["message"]

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    r = chat(client, raw)
    assert r.status_code == 502 and "segfault-ish" in r.json()["error"]["message"]

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    runtime_settings.set_many(db, {"request_timeout_s": 1})
    r = chat(client, raw)
    assert r.status_code == 504 and r.json()["error"]["code"] == "timeout"
    assert db.query(RequestLog).filter_by(status="error").count() == 3


def test_log_bodies_setting(client, make_token, db):
    runtime_settings.set_many(db, {"log_bodies": True})
    raw, _ = make_token()
    chat(client, raw)
    log = db.query(RequestLog).one()
    assert "hi" in log.prompt and log.response == "echo[sonnet|medium]: hi"


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
