"""Turns docs/openapi.json into the view model behind the /docs page.

The spec is the single source of truth: the page, /openapi.json and /openapi.yaml all come from the
files in docs/, so the reference cannot drift from what is published.
"""

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from markupsafe import Markup

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
SPEC_JSON = DOCS_DIR / "openapi.json"
SPEC_YAML = DOCS_DIR / "openapi.yaml"
AGENT_GUIDE = DOCS_DIR / "agent-api-guide.md"

# Base URL used throughout the spec's examples; the page swaps in the server's real address.
SPEC_BASE_URL = "http://localhost:8000"

_METHODS = ("get", "post", "put", "patch", "delete")


def agent_guide(base_url: str) -> str:
    """docs/agent-api-guide.md with the server's real address in place of the placeholder."""
    return AGENT_GUIDE.read_text().replace(SPEC_BASE_URL, base_url.rstrip("/"))


@lru_cache
def load_spec() -> dict:
    return json.loads(SPEC_JSON.read_text())


# --- tiny Markdown for spec descriptions --------------------------------------


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown(text: str | None) -> Markup:
    """Paragraphs, `- ` lists, inline code, bold and links: all the spec's descriptions use."""
    if not text:
        return Markup("")
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{_inline(line.lstrip()[2:])}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
    return Markup("".join(out))


def breakable(name: str) -> Markup:
    """A field path like `a.b[].c` that may wrap after each separator."""
    escaped = html.escape(name)
    return Markup(re.sub(r"(\.|\[\])", r"\1<wbr>", escaped))


# --- schema flattening --------------------------------------------------------


def _resolve(spec: dict, node: dict) -> tuple[dict, str | None]:
    """Follow a local $ref. Returns (schema, component name or None)."""
    ref = node.get("$ref")
    if not ref:
        return node, None
    target: Any = spec
    for part in ref.lstrip("#/").split("/"):
        target = target[part]
    return target, ref.rsplit("/", 1)[-1]


def type_label(spec: dict, schema: dict) -> str:
    schema, name = _resolve(spec, schema)
    if "const" in schema:
        return json.dumps(schema["const"])
    if "oneOf" in schema:
        return " | ".join(type_label(spec, s) for s in schema["oneOf"])
    if "allOf" in schema:
        return name or "object"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(kind)
    if kind == "array":
        return f"array<{type_label(spec, schema.get('items', {}))}>"
    if name and kind in ("object", None):
        return name
    label = kind or "any"
    if schema.get("format"):
        label += f" ({schema['format']})"
    return label


def _properties(spec: dict, schema: dict) -> tuple[dict, set]:
    schema, _ = _resolve(spec, schema)
    props, required = dict(schema.get("properties", {})), set(schema.get("required", []))
    for part in schema.get("allOf", []):
        p, r = _properties(spec, part)
        props.update(p)
        required |= r
    return props, required


def _nested(spec: dict, schema: dict, via_array: bool = False) -> tuple[dict, bool] | None:
    """The object schema whose fields deserve their own rows, and whether it sits inside an array.

    Looks through arrays and `oneOf` branches, so `content: string | array<ContentPart>` yields
    ContentPart's fields as `content[].type`.
    """
    schema, _ = _resolve(spec, schema)
    if schema.get("type") == "array":
        return _nested(spec, schema.get("items", {}), True)
    for branch in schema.get("oneOf", []):
        found = _nested(spec, branch, via_array)
        if found:
            return found
    props, _ = _properties(spec, schema)
    return (schema, via_array) if props else None


def fields(spec: dict, schema: dict, prefix: str = "", depth: int = 0) -> list[dict]:
    """Flatten an object schema into rows like `messages[].role`, for the field tables."""
    props, required = _properties(spec, schema)
    rows = []
    for name, prop in props.items():
        resolved, _ = _resolve(spec, prop)
        path = f"{prefix}{name}"
        rows.append(
            {
                "name": path,
                "type": type_label(spec, prop),
                "required": name in required,
                "description": markdown(resolved.get("description")),
                "enum": resolved.get("enum"),
                "default": resolved.get("default"),
            }
        )
        nested = _nested(spec, prop)
        if nested is not None and depth < 3:
            child, in_array = nested
            rows += fields(spec, child, f"{path}[]." if in_array else f"{path}.", depth + 1)
    return rows


# --- page model ---------------------------------------------------------------


def _example_json(value: Any, base_url: str) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False).replace(SPEC_BASE_URL, base_url)


def _examples(media: dict, base_url: str) -> list[dict]:
    if "examples" in media:
        return [
            {"summary": ex.get("summary") or key, "json": _example_json(ex["value"], base_url)}
            for key, ex in media["examples"].items()
        ]
    if "example" in media:
        return [{"summary": "Example", "json": _example_json(media["example"], base_url)}]
    return []


def _operation(spec: dict, path: str, method: str, op: dict, path_item: dict, base_url: str) -> dict:
    params = []
    for raw in path_item.get("parameters", []) + op.get("parameters", []):
        p, _ = _resolve(spec, raw)
        schema = p.get("schema", {})
        params.append(
            {
                "name": p["name"],
                "in": p["in"],
                "required": p.get("required", False),
                "type": type_label(spec, schema),
                "description": markdown(p.get("description")),
                "default": schema.get("default"),
            }
        )

    body = None
    if "requestBody" in op:
        media = op["requestBody"]["content"]["application/json"]
        body = {
            "schema_name": _resolve(spec, media["schema"])[1],
            "fields": fields(spec, media["schema"]),
            "examples": _examples(media, base_url),
        }

    responses = []
    for status, raw in op.get("responses", {}).items():
        resp, _ = _resolve(spec, raw)
        content = resp.get("content", {})
        media_type, media = next(iter(content.items()), (None, {}))
        schema = media.get("schema")
        responses.append(
            {
                "status": status,
                "ok": status.startswith("2"),
                "description": markdown(resp.get("description")),
                "media_type": media_type,
                "schema": type_label(spec, schema) if schema else None,
                "examples": _examples(media, base_url),
            }
        )

    samples = [
        {"label": s["label"], "lang": s["lang"], "source": s["source"].replace(SPEC_BASE_URL, base_url).rstrip()}
        for s in op.get("x-codeSamples", [])
    ]
    return {
        "id": op["operationId"],
        "method": method.upper(),
        "path": path,
        "summary": op.get("summary", ""),
        "description": markdown(op.get("description")),
        "public": op.get("security") == [],
        "params": params,
        "body": body,
        "responses": responses,
        "samples": samples,
    }


def page_model(base_url: str) -> dict:
    spec = load_spec()
    base_url = base_url.rstrip("/")
    groups = {t["name"]: {"name": t["name"], "description": markdown(t.get("description")), "ops": []} for t in spec["tags"]}
    for path, item in spec["paths"].items():
        for method in _METHODS:
            if method in item:
                op = item[method]
                groups[op["tags"][0]]["ops"].append(_operation(spec, path, method, op, item, base_url))

    schemas = [
        {
            "name": name,
            "description": markdown(schema.get("description")),
            "type": type_label(spec, schema) if not _properties(spec, schema)[0] else "object",
            "enum": schema.get("enum"),
            "fields": fields(spec, schema),
        }
        for name, schema in spec["components"]["schemas"].items()
    ]
    return {
        "info": spec["info"],
        "version": spec["info"]["version"],
        "groups": [g for g in groups.values() if g["ops"]],
        "schemas": schemas,
        "base_url": base_url,
        "guide": guide_samples(base_url),
    }


# --- guide examples (the narrative sections above the reference) -----------------


def _s(label: str, lang: str, source: str) -> dict:
    return {"label": label, "lang": lang, "source": source.strip("\n")}


GUIDE_SAMPLES: dict[str, list[dict]] = {
    "quickstart": [
        _s("cURL", "bash", """
curl http://localhost:8000/v1/chat/completions \\
  -H "Authorization: Bearer $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello, Claude!"}]}'
"""),
        _s("Python (openai)", "python", """
# pip install openai
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...")

reply = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "Hello, Claude!"}],
)
print(reply.choices[0].message.content)
"""),
        _s("JavaScript (openai)", "javascript", """
// npm install openai
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8000/v1", apiKey: process.env.API_KEY });

const reply = await client.chat.completions.create({
  model: "sonnet",
  messages: [{ role: "user", content: "Hello, Claude!" }],
});
console.log(reply.choices[0].message.content);
"""),
    ],
    "auth": [
        _s("cURL", "bash", """
# Preferred
curl http://localhost:8000/v1/models -H "Authorization: Bearer chk_..."

# Equivalent, for clients that cannot set Authorization
curl http://localhost:8000/v1/models -H "x-api-key: chk_..."
"""),
        _s("Python (requests)", "python", """
import os
import requests

session = requests.Session()
session.headers["Authorization"] = f"Bearer {os.environ['API_KEY']}"
print(session.get("http://localhost:8000/v1/models").json())
"""),
        _s("JavaScript (fetch)", "javascript", """
const res = await fetch("http://localhost:8000/v1/models", {
  headers: { Authorization: `Bearer ${process.env.API_KEY}` },
});
console.log(await res.json());
"""),
    ],
    "effort": [
        _s("cURL", "bash", """
curl http://localhost:8000/v1/chat/completions \\
  -H "Authorization: Bearer $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "opus",
    "effort": "max",
    "messages": [{"role": "user", "content": "Prove that there are infinitely many primes."}]
  }'
"""),
        _s("Python (openai)", "python", """
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...")

# OpenAI's own field covers low / medium / high ...
fast = client.chat.completions.create(
    model="haiku",
    reasoning_effort="low",
    messages=[{"role": "user", "content": "Summarise: the cat sat on the mat."}],
)

# ... and `effort` (sent through extra_body) reaches every Claude level, including xhigh and max.
deep = client.chat.completions.create(
    model="opus",
    messages=[{"role": "user", "content": "Prove that there are infinitely many primes."}],
    extra_body={"effort": "max"},
)
"""),
        _s("JavaScript (openai)", "javascript", """
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8000/v1", apiKey: process.env.API_KEY });

const deep = await client.chat.completions.create({
  model: "opus",
  messages: [{ role: "user", content: "Prove that there are infinitely many primes." }],
  effort: "max", // not in OpenAI's types, but sent through as-is
});
"""),
    ],
    "sessions": [
        _s("Python (requests)", "python", """
import requests

BASE = "http://localhost:8000/v1"
HEADERS = {"Authorization": "Bearer chk_..."}

# 1. Create the session once; the system prompt is fixed for its lifetime.
session = requests.post(
    f"{BASE}/sessions",
    json={"model": "sonnet", "system_prompt": "You are a patient maths tutor."},
    headers=HEADERS,
).json()

# 2. Each turn sends only the new message; Claude resumes the same conversation.
def ask(text: str, **overrides) -> str:
    r = requests.post(
        f"{BASE}/sessions/{session['id']}/messages",
        json={"content": text, **overrides},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

print(ask("My name is Amina. What is 1/2 + 1/4?"))
print(ask("What is my name, and what was my last question?"))
print(ask("Give me a harder one.", model="opus", effort="high"))  # switch per turn

# 3. Read it back, or delete it when done.
history = requests.get(f"{BASE}/sessions/{session['id']}", headers=HEADERS).json()["messages"]
requests.delete(f"{BASE}/sessions/{session['id']}", headers=HEADERS)
"""),
        _s("Python (openai)", "python", """
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...")

# "new" starts a session; the response carries its id.
first = client.chat.completions.create(
    model="sonnet",
    messages=[
        {"role": "system", "content": "You are a patient maths tutor."},
        {"role": "user", "content": "My name is Amina. What is 1/2 + 1/4?"},
    ],
    extra_body={"session_id": "new"},
)
session_id = first.model_extra["session_id"]

# Continue it: send only the newest user message.
second = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "What is my name?"}],
    extra_body={"session_id": session_id},
)
print(second.choices[0].message.content)
"""),
        _s("JavaScript (fetch)", "javascript", """
const BASE = "http://localhost:8000/v1";
const headers = { Authorization: `Bearer ${process.env.API_KEY}`, "Content-Type": "application/json" };

const session = await (
  await fetch(`${BASE}/sessions`, {
    method: "POST",
    headers,
    body: JSON.stringify({ model: "sonnet", system_prompt: "You are a patient maths tutor." }),
  })
).json();

async function ask(content) {
  const res = await fetch(`${BASE}/sessions/${session.id}/messages`, {
    method: "POST",
    headers,
    body: JSON.stringify({ content }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error.message);
  return data.choices[0].message.content;
}

console.log(await ask("My name is Amina. What is 1/2 + 1/4?"));
console.log(await ask("What is my name?"));
"""),
    ],
    "errors": [
        _s("Python (openai)", "python", """
import time
import openai
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...", max_retries=0)

def complete(prompt: str, attempts: int = 3) -> str:
    for attempt in range(attempts):
        try:
            r = client.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": prompt}])
            return r.choices[0].message.content
        except openai.RateLimitError:            # 429 rate_limit_exceeded: wait and retry
            time.sleep(10 * (attempt + 1))
        except openai.AuthenticationError:       # 401: missing, invalid, revoked or expired key
            raise
        except openai.PermissionDeniedError as e:  # 403: model/effort not allowed, user disabled
            raise RuntimeError(e.body["code"]) from e
        except openai.APIStatusError as e:       # 502 upstream_error, 504 timeout
            if e.status_code in (502, 504) and attempt < attempts - 1:
                continue
            raise
    raise RuntimeError("gave up")
"""),
        _s("JavaScript (fetch)", "javascript", """
const res = await fetch("http://localhost:8000/v1/chat/completions", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.API_KEY}`, "Content-Type": "application/json" },
  body: JSON.stringify({ model: "sonnet", messages: [{ role: "user", content: "Hi" }] }),
});
const body = await res.json();
if (!res.ok) {
  // { error: { message, type, param, code } }
  const { code, message } = body.error;
  if (res.status === 429) console.warn("Slow down:", message);
  else throw new Error(`${res.status} ${code}: ${message}`);
}
"""),
    ],
}


def guide_samples(base_url: str) -> dict[str, list[dict]]:
    base_url = base_url.rstrip("/")
    return {
        key: [{**s, "source": s["source"].replace(SPEC_BASE_URL, base_url)} for s in samples]
        for key, samples in GUIDE_SAMPLES.items()
    }
