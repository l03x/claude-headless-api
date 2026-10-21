import asyncio
import json
import time

import pytest
from openai import OpenAI

from app import runtime_settings
from app.claude_runner import gate, stream_claude, transcript_paths
from app.models import ChatMessage, ChatSession, RequestLog

ECHO = "echo[haiku|low]: hi"


def H(raw):
    return {"Authorization": f"Bearer {raw}"}


def sse_data(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


def stream_chat(client, raw, **body):
    body.setdefault("model", "haiku")
    body.setdefault("effort", "low")
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return client.post("/v1/chat/completions", json={"stream": True, **body}, headers=H(raw))


def content_of(events: list[str]) -> str:
    chunks = [json.loads(e) for e in events if e != "[DONE]"]
    return "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c.get("choices"))


def test_stateless_stream(client, make_token, tmp_path, monkeypatch):
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    raw, _ = make_token()
    r = stream_chat(client, raw)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_data(r.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" and c["id"] == chunks[0]["id"] for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[-1]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop", "logprobs": None}
    assert len(chunks) > 4  # the reply arrived in several pieces, not one
    assert content_of(events) == ECHO
    assert "SECRET-THOUGHT" not in r.text  # thinking deltas are never forwarded
    argv = json.loads(args_file.read_text())["argv"]
    assert {"stream-json", "--verbose", "--include-partial-messages"} <= set(argv)


def test_include_usage(client, make_token):
    raw, _ = make_token()
    events = sse_data(stream_chat(client, raw, stream_options={"include_usage": True}).text)
    usage_chunk = json.loads(events[-2])
    assert usage_chunk["choices"] == [] and usage_chunk["usage"]["prompt_tokens"] == 113


def test_log_row_written_after_stream(client, make_token, db):
    raw, token = make_token()
    stream_chat(client, raw)
    log = db.query(RequestLog).one()
    assert (log.status, log.http_status, log.stream, log.user_id) == ("ok", 200, True, token.user_id)
    assert (log.input_tokens, log.output_tokens, log.cost_usd) == (10, 5, 0.0123) and log.latency_ms is not None


def test_openai_sdk_streams(client, make_token):
    raw, _ = make_token()
    sdk = OpenAI(base_url="http://testserver/v1", api_key=raw, http_client=client, max_retries=0)
    chunks = list(sdk.chat.completions.create(model="haiku", messages=[{"role": "user", "content": "hi"}],
                                             stream=True, extra_body={"effort": "low"}))  # fmt: skip
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == ECHO

    first = list(sdk.chat.completions.create(model="haiku", messages=[{"role": "user", "content": "one"}],
                                            stream=True, extra_body={"session_id": "new", "effort": "low"}))  # fmt: skip
    session_id = first[0].model_extra["session_id"]
    second = sdk.chat.completions.create(model="haiku", messages=[{"role": "user", "content": "two"}],
                                         stream=True, extra_body={"session_id": session_id, "effort": "low"})  # fmt: skip
    assert "".join(c.choices[0].delta.content or "" for c in second if c.choices) == "echo[haiku|low]#2: two"


def test_session_messages_stream(client, make_token, db):
    raw, _ = make_token()
    sid = client.post("/v1/sessions", json={"model": "haiku", "effort": "low"}, headers=H(raw)).json()["id"]
    for n, text in ((1, "one"), (2, "two")):
        r = client.post(f"/v1/sessions/{sid}/messages", json={"content": text, "stream": True}, headers=H(raw))
        events = sse_data(r.text)
        assert content_of(events) == f"echo[haiku|low]#{n}: {text}"  # turn 2 resumed the Claude session
        assert all(json.loads(e)["session_id"] == sid for e in events[:-1])
    stored = client.get(f"/v1/sessions/{sid}", headers=H(raw)).json()["messages"]
    assert [m["content"] for m in stored] == ["one", "echo[haiku|low]#1: one", "two", "echo[haiku|low]#2: two"]


def test_json_schema_stream_sends_the_document_once(client, make_token):
    raw, _ = make_token()
    rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
    events = sse_data(stream_chat(client, raw, response_format=rf).text)
    content_chunks = [e for e in events[:-1] if json.loads(e)["choices"][0]["delta"].get("content")]
    assert len(content_chunks) == 1 and json.loads(content_of(events)) == {"echo": "hi"}


def test_errors_before_output_are_plain_json(client, make_token, monkeypatch):
    raw, _ = make_token()
    assert stream_chat(client, "chk_wrong").status_code == 401
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    r = stream_chat(client, raw)
    assert r.status_code == 502 and r.headers["content-type"].startswith("application/json")
    assert "Failed to authenticate" in r.json()["error"]["message"]


def test_failure_mid_stream_is_an_error_event(client, make_token, db, monkeypatch):
    raw, _ = make_token()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail_midway")
    r = stream_chat(client, raw)
    assert r.status_code == 200
    events = sse_data(r.text)
    assert "[DONE]" not in events
    error = json.loads(events[-1])["error"]
    assert error["code"] == "upstream_error" and "connection reset" in error["message"]
    assert content_of(events[:-1])  # some text had already been sent
    log = db.query(RequestLog).one()
    assert (log.status, log.http_status) == ("error", 502)


def test_timeout_mid_stream(client, make_token, db, monkeypatch):
    raw, _ = make_token()
    runtime_settings.set_many(db, {"request_timeout_s": 1})
    monkeypatch.setenv("FAKE_CLAUDE_DELTA_DELAY_MS", "400")
    events = sse_data(stream_chat(client, raw).text)
    assert json.loads(events[-1])["error"]["code"] == "timeout"


def test_new_session_whose_first_streamed_turn_fails_is_removed(client, make_token, db, monkeypatch):
    raw, _ = make_token()
    body = {"session_id": "new", "messages": [{"role": "user", "content": "x"}]}
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    assert stream_chat(client, raw, **body).status_code == 502  # fails before output
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail_midway")
    assert "error" in sse_data(stream_chat(client, raw, **body).text)[-1]  # fails part way
    db.expire_all()
    assert db.query(ChatSession).count() == 0


# --- client disconnects ----------------------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_disconnect_stops_claude_and_logs_499(client, make_token, db, monkeypatch):
    """Closing the stream part way (what a client disconnect does) kills the run and logs it."""
    from app.routers.api import _send_stream, _stream_reply

    monkeypatch.setenv("FAKE_CLAUDE_DELTA_DELAY_MS", "500")
    settings = runtime_settings.get_all(db)

    async def scenario():
        log = RequestLog(endpoint="chat.completions", status="error", http_status=500)
        events = stream_claude("hello there, a long enough prompt", model="haiku", effort="low",
                               system_prompt="s", timeout_s=30)  # fmt: skip
        stream = await _stream_reply(events, log=log, settings=settings, model="haiku", include_usage=False,
                                     prompt_for_log=None)  # fmt: skip
        body = _send_stream(stream, log, time.monotonic())
        await body.__anext__()  # role chunk
        await body.__anext__()  # first text
        started = time.monotonic()
        await body.aclose()  # the client goes away
        return log, time.monotonic() - started

    log, closing_took = _run(scenario())
    assert closing_took < 2  # killed, not run to the end (the fake would need several more seconds)
    assert (log.status, log.http_status) == ("error", 499) and log.stream is True
    db.expire_all()
    assert db.query(RequestLog).filter_by(http_status=499).count() == 1
    assert gate.in_flight == 0 and gate.queued == 0  # the slot was given back


def test_disconnect_mid_session_turn_stores_nothing_and_rebuilds(client, make_token, db, monkeypatch):
    from app.sessions import stream_turn

    raw, _ = make_token()
    sid = client.post("/v1/sessions", json={"model": "haiku", "effort": "low"}, headers=H(raw)).json()["id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"content": "first"}, headers=H(raw))  # turn 1, stored
    db.expire_all()
    before = db.get(ChatSession, sid).claude_session_id
    assert transcript_paths(before)

    monkeypatch.setenv("FAKE_CLAUDE_DELTA_DELAY_MS", "500")
    settings = runtime_settings.get_all(db)

    async def scenario():
        session = db.get(ChatSession, sid)
        turn = stream_turn(db, session, "second", model="haiku", effort="low", settings=settings)
        await turn.__anext__()
        await turn.aclose()

    _run(scenario())
    db.expire_all()
    session = db.get(ChatSession, sid)
    assert session.claude_session_id is None and not transcript_paths(before)
    assert db.query(ChatMessage).filter_by(session_id=sid).count() == 2  # only turn 1
    assert gate.in_flight == 0

    # The next turn rebuilds the conversation from the stored messages.
    monkeypatch.delenv("FAKE_CLAUDE_DELTA_DELAY_MS")
    r = client.post(f"/v1/sessions/{sid}/messages", json={"content": "third"}, headers=H(raw)).json()
    assert r["choices"][0]["message"]["content"].startswith("echo[haiku|low]#1: <conversation>")


def test_gate_releases_slots_even_when_cancelled():
    from app.claude_runner import ConcurrencyGate

    async def scenario():
        g = ConcurrencyGate(1)
        await g.__aenter__()  # holder
        waiter = asyncio.ensure_future(g.__aenter__())
        await asyncio.sleep(0)
        assert g.queued == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert g.queued == 0 and g.in_flight == 1
        await g.__aexit__(None, None, None)
        assert g.in_flight == 0

    _run(scenario())


# --- dashboard chat -------------------------------------------------------------------


def test_dashboard_chat_streams_and_stores(admin_client, db):
    from tests.conftest import client_csrf

    csrf = client_csrf(admin_client, "/admin/chat")
    sid = admin_client.post("/admin/chat/new", data={"csrf_token": csrf, "model": "haiku", "effort": "low"}).json()["id"]
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "hi", "stream": "1"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(e) for e in sse_data(r.text)]
    assert "".join(e.get("delta", "") for e in events) == ECHO.replace("low]", "low]#1")
    done = events[-1]
    assert done["done"] is True and done["title"] == "hi" and done["meta"]["model"] == "haiku"
    db.expire_all()
    assert db.get(ChatSession, sid).message_count == 2
    log = db.query(RequestLog).filter_by(endpoint="dashboard.chat").one()
    assert (log.status, log.stream) == ("ok", True)


def test_dashboard_chat_stream_errors(admin_client, db, monkeypatch):
    from tests.conftest import client_csrf

    csrf = client_csrf(admin_client, "/admin/chat")
    new = lambda: admin_client.post("/admin/chat/new", data={"csrf_token": csrf}).json()["id"]  # noqa: E731
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    sid = new()
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "x", "stream": "1"})
    assert r.status_code == 502 and r.json()["discarded"] is True  # failed before any text: plain JSON
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail_midway")
    sid = new()
    r = admin_client.post(f"/admin/chat/{sid}/send", data={"csrf_token": csrf, "content": "x", "stream": "1"})
    last = json.loads(sse_data(r.text)[-1])
    assert "connection reset" in last["error"] and last["discarded"] is True
    db.expire_all()
    assert db.query(ChatSession).count() == 0
