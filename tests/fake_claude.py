#!/usr/bin/env python3
"""Stand-in for the `claude` CLI. Echoes what it was given in CLI-shaped JSON.

Behaviour switches (env): FAKE_CLAUDE_MODE = ok | error | crash | sleep | fail_midway, FAKE_CLAUDE_ARGS_FILE,
FAKE_CLAUDE_LATENCY_MS = "<ms>" or "<min>-<max>" (simulated thinking time, used by the load tests),
FAKE_CLAUDE_ERROR_RATE = 0..1 (fraction of runs that fail like an upstream error),
FAKE_CLAUDE_DELTA_DELAY_MS = pause between streamed text deltas.

With `--output-format stream-json` it prints stream-json lines like the real CLI: it requires --verbose,
and with --include-partial-messages emits `stream_event` deltas (a thinking block, then the text in pieces).

With `--tools WebSearch` (and `--allowedTools WebSearch`, as print mode needs) it makes one search: streamed,
that is a first turn saying "Searching the web." and calling the tool, then the answer in a second turn.
Resuming a transcript that holds a search with `--tools ""` fails the way the API does.
"""

import json
import os
import random
import sys
import time


def flag(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return None


if "--version" in sys.argv:
    print("9.9.9 (Fake Claude)")
    sys.exit(0)

stream = flag("--output-format") == "stream-json"
if stream and "--verbose" not in sys.argv:
    print("Error: When using --print, --output-format=stream-json requires --verbose", file=sys.stderr)
    sys.exit(1)

prompt = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_ARGS_FILE"):
    with open(os.environ["FAKE_CLAUDE_ARGS_FILE"], "w") as f:
        json.dump({"argv": sys.argv[1:], "stdin": prompt, "cwd": os.getcwd(), "env": sorted(os.environ)}, f)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

latency = os.environ.get("FAKE_CLAUDE_LATENCY_MS")
if latency:
    low, _, high = latency.partition("-")
    time.sleep(random.uniform(float(low), float(high or low)) / 1000)
if random.random() < float(os.environ.get("FAKE_CLAUDE_ERROR_RATE") or 0):
    mode = "error"

searching = "WebSearch" in (flag("--tools") or "").split(",")
if searching and "WebSearch" not in (flag("--allowedTools") or "").split(","):
    mode = "error"  # print mode: nobody answers the permission prompt, so the run can't search

# Session emulation: transcripts live where the real CLI keeps them.
session_id = flag("--session-id") or flag("--resume") or "11111111-2222-3333-4444-555555555555"
suffix = ""
if flag("--session-id") or flag("--resume"):
    project = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", os.getcwd().replace("/", "-"))
    path = os.path.join(project, session_id + ".jsonl")
    if flag("--resume") and not os.path.exists(path):
        print(json.dumps({"type": "result", "is_error": True, "result": f"No conversation found with session ID: {session_id}"}))
        sys.exit(1)
    if flag("--resume") and not searching:
        with open(path) as f:
            if any(json.loads(line).get("web_search") for line in f):
                print(json.dumps({"type": "result", "is_error": True, "result": "API Error: 400 Requests which include `tool_use` or `tool_result` blocks must define tools."}))
                sys.exit(1)
    os.makedirs(project, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"prompt": prompt, "web_search": searching}) + "\n")
    with open(path) as f:
        suffix = f"#{sum(1 for _ in f)}"
if mode == "sleep":
    time.sleep(30)
if mode == "crash":
    print("segfault-ish", file=sys.stderr)
    sys.exit(3)

result = {
    "type": "result",
    "subtype": "success",
    "is_error": mode == "error",
    "result": "Failed to authenticate" if mode == "error" else f"echo[{flag('--model')}|{flag('--effort')}]{suffix}: {prompt}",
    "session_id": session_id,
    "total_cost_usd": 0.0123,
    "duration_ms": 42,
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 3},
}
if searching:
    result["usage"]["server_tool_use"] = {"web_search_requests": 1 if mode != "error" else 0}
if flag("--json-schema") is not None and mode != "error":
    result["structured_output"] = {"echo": prompt}

if not stream:
    print(json.dumps(result))
    sys.exit(1 if mode == "error" else 0)


def emit(obj):
    print(json.dumps(obj), flush=True)


def event(e):
    emit({"type": "stream_event", "event": e, "session_id": session_id, "parent_tool_use_id": None, "uuid": "fake"})


text = result["result"]
emit({"type": "system", "subtype": "init", "session_id": session_id, "model": flag("--model")})
if mode != "error" and searching and "--include-partial-messages" in sys.argv:
    event({"type": "message_start", "message": {"id": "msg_search", "role": "assistant", "content": []}})
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Searching the web."}})
    event({"type": "content_block_stop", "index": 0})
    event({"type": "content_block_start", "index": 1,
           "content_block": {"type": "tool_use", "id": "toolu_fake", "name": "WebSearch", "input": {}}})
    event({"type": "content_block_stop", "index": 1})
    event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
    event({"type": "message_stop"})
    emit({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_fake", "content": "Web search results: …"}]},
        "session_id": session_id, "parent_tool_use_id": None})
if mode != "error" and "--include-partial-messages" in sys.argv:
    delay = float(os.environ.get("FAKE_CLAUDE_DELTA_DELAY_MS") or 0) / 1000
    event({"type": "message_start", "message": {"id": "msg_fake", "role": "assistant", "content": []}})
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "SECRET-THOUGHT"}})
    event({"type": "content_block_stop", "index": 0})
    event({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
    pieces = [text[i : i + 8] for i in range(0, len(text), 8)]
    for i, piece in enumerate(pieces):
        event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}})
        if mode == "fail_midway" and i == 1:
            print("connection reset by peer", file=sys.stderr)
            sys.exit(1)
        if delay:
            time.sleep(delay)
    event({"type": "content_block_stop", "index": 1})
    event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    event({"type": "message_stop"})
emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
      "session_id": session_id, "parent_tool_use_id": None})
emit(result)
sys.exit(1 if mode == "error" else 0)
