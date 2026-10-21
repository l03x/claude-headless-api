#!/usr/bin/env python3
"""Stand-in for the `claude` CLI. Echoes what it was given in CLI-shaped JSON.

Behaviour switches (env): FAKE_CLAUDE_MODE = ok | error | crash | sleep, FAKE_CLAUDE_ARGS_FILE,
FAKE_CLAUDE_LATENCY_MS = "<ms>" or "<min>-<max>" (simulated thinking time, used by the load tests),
FAKE_CLAUDE_ERROR_RATE = 0..1 (fraction of runs that fail like an upstream error).
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

# Session emulation: transcripts live where the real CLI keeps them.
session_id = flag("--session-id") or flag("--resume") or "11111111-2222-3333-4444-555555555555"
suffix = ""
if flag("--session-id") or flag("--resume"):
    project = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", os.getcwd().replace("/", "-"))
    path = os.path.join(project, session_id + ".jsonl")
    if flag("--resume") and not os.path.exists(path):
        print(json.dumps({"type": "result", "is_error": True, "result": f"No conversation found with session ID: {session_id}"}))
        sys.exit(1)
    os.makedirs(project, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"prompt": prompt}) + "\n")
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
print(json.dumps(result))
sys.exit(1 if mode == "error" else 0)
