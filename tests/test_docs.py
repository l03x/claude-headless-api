"""The OpenAPI spec in docs/ must stay valid, complete and true to what the server returns."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_openapi
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
SPEC = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource("urn:spec", Resource(contents=SPEC, specification=DRAFT202012))
METHODS = {"get", "post", "put", "patch", "delete"}


def check(instance, ref: str) -> None:
    """Validate against a spec schema given as a JSON pointer like '#/components/schemas/Session'."""
    Draft202012Validator({"$ref": f"urn:spec{ref}"}, registry=REGISTRY).validate(instance)


def schema_ref(schema: dict, pointer: str) -> str:
    return schema.get("$ref", pointer)


def test_spec_is_valid_openapi():
    validate_openapi(SPEC)


def test_json_copy_is_in_sync():
    result = subprocess.run([sys.executable, "scripts/export_openapi.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_route_is_documented(client):
    documented = {(path, m) for path, item in SPEC["paths"].items() for m in item if m in METHODS}
    served = set()
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v1/") or path in ("/healthz", "/openapi.json", "/openapi.yaml", "/llms.txt"):
            served |= {(path, m.lower()) for m in route.methods if m.lower() in METHODS}
    assert documented == served


def test_every_operation_has_docs_and_code_samples():
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method not in METHODS:
                continue
            assert op.get("summary") and op.get("description"), f"{method} {path}"
            langs = {s["lang"] for s in op.get("x-codeSamples", [])}
            assert "bash" in langs, f"{method} {path} has no cURL sample"
            if path.startswith("/v1/"):
                assert {"python", "javascript"} <= langs, f"{method} {path}"


def _examples():
    """Every example in the spec, with the schema pointer it must satisfy."""
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method not in METHODS:
                continue
            ptr = f"#/paths/{path.replace('~', '~0').replace('/', '~1')}/{method}"
            media = op.get("requestBody", {}).get("content", {}).get("application/json")
            if media:
                yield from _media_examples(media, f"{ptr}/requestBody/content/application~1json/schema", f"{method} {path} request")
            for status, resp in op["responses"].items():
                rptr = f"{ptr}/responses/{status}"
                if "$ref" in resp:
                    rptr = resp["$ref"]
                    resp = SPEC["components"]["responses"][rptr.rsplit("/", 1)[-1]]
                media = resp.get("content", {}).get("application/json")
                if media:
                    yield from _media_examples(media, f"{rptr}/content/application~1json/schema", f"{method} {path} {status}")


def _media_examples(media, pointer, label):
    ref = schema_ref(media["schema"], pointer)
    if "example" in media:
        yield pytest.param(media["example"], ref, id=label)
    for name, ex in media.get("examples", {}).items():
        yield pytest.param(ex["value"], ref, id=f"{label} {name}")


@pytest.mark.parametrize("example,ref", list(_examples()))
def test_examples_match_their_schemas(example, ref):
    check(example, ref)


# --- real responses match the documented schemas ------------------------------


def H(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_live_responses_match_spec(client, make_token, monkeypatch):
    raw, _ = make_token()
    S = "#/components/schemas/"

    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=H(raw))
    check(r.json(), S + "ChatCompletion")
    check(client.get("/v1/models", headers=H(raw)).json(), S + "ModelList")

    session = client.post("/v1/sessions", json={"title": "t"}, headers=H(raw))
    assert session.status_code == 201
    check(session.json(), S + "Session")
    sid = session.json()["id"]
    reply = client.post(f"/v1/sessions/{sid}/messages", json={"content": "hi"}, headers=H(raw)).json()
    check(reply, S + "ChatCompletion")
    assert "session_id" in reply
    check(client.get(f"/v1/sessions/{sid}", headers=H(raw)).json(), S + "SessionWithMessages")
    check(client.get("/v1/sessions", headers=H(raw)).json(), S + "SessionList")
    check(client.delete(f"/v1/sessions/{sid}", headers=H(raw)).json(), S + "SessionDeleted")

    errors = [
        client.get("/v1/models"),
        client.post("/v1/chat/completions", json={"model": "nope", "messages": [{"role": "user", "content": "x"}]}, headers=H(raw)),
        client.get("/v1/sessions/missing", headers=H(raw)),
    ]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    errors.append(client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}, headers=H(raw)))
    assert [e.status_code for e in errors] == [401, 400, 404, 502]
    for e in errors:
        check(e.json(), S + "Error")


def test_docs_page_and_spec_endpoints(client):
    page = client.get("/docs")
    assert page.status_code == 200
    html = page.text
    for op_id in ("createChatCompletion", "sendSessionMessage", "listModels", "healthCheck"):
        assert f'id="{op_id}"' in html
    assert 'class="code-tabs"' in html and "from openai import OpenAI" in html
    assert "http://testserver/v1/chat/completions" in html  # examples point at the real server
    assert client.get("/openapi.json").json()["openapi"] == "3.1.0"
    assert client.get("/openapi.yaml").text.startswith("openapi: 3.1.0")
    assert client.get("/redoc").status_code == 404
    guide = client.get("/llms.txt")
    assert guide.headers["content-type"].startswith("text/markdown")
    assert guide.text.startswith("# Claude Headless API") and "http://testserver/v1" in guide.text
    assert "localhost:8000" not in guide.text
    assert "swagger" not in html.lower()


# --- Markdown docs stay truthful -----------------------------------------------

MARKDOWN = [
    ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md", ROOT / "grafana" / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
]  # fmt: skip


@pytest.mark.parametrize("doc", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_markdown_links_resolve(doc):
    import re

    for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = target.split("#", 1)[0]
        assert (doc.parent / path).exists(), f"{doc.name}: broken link {target}"


def test_agents_md_repo_map_paths_exist():
    import re

    text = (ROOT / "AGENTS.md").read_text()
    table = text[text.index("## Repository map") : text.index("## Conventions")]
    for cell in re.findall(r"^\| (`[^|]+`) \|", table, flags=re.M):
        for path in re.findall(r"`([^`]+)`", cell):
            assert (ROOT / path.rstrip("/")).exists(), f"AGENTS.md maps a missing path: {path}"


def test_dashboard_queries_use_existing_metrics():
    """Every app metric the Grafana dashboard queries must still be emitted by app/telemetry.py."""
    import json
    import re

    source = (ROOT / "app" / "telemetry.py").read_text()
    emitted = {name.replace(".", "_") for name in re.findall(r'create_\w+\(\s*"([a-z_.]+)"', source)}
    assert {"claude_runs", "claude_run_duration", "api_requests"} <= emitted

    dashboard = json.loads((ROOT / "grafana" / "dashboards" / "claude-headless-api.json").read_text())
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", []) if "expr" in t and t["datasource"]["type"] == "prometheus"]
    assert exprs
    used = {m for e in exprs for m in re.findall(r"\b((?:claude|api)_[a-z_]+)\{", e)}  # metric{…}, not labels
    assert len(used) >= 8
    suffixes = re.compile(r"(_seconds)?(_total|_bucket|_sum|_count)?$")
    missing = sorted(m for m in used if suffixes.sub("", m) not in emitted)
    assert not missing, f"dashboard queries metrics the app no longer emits: {missing}"
