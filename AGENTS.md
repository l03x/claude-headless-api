# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository. Read this first, then the doc
that matches your task:

| Task | Read |
|---|---|
| Understand how a request flows, the data model, sessions, security | [docs/architecture.md](docs/architecture.md) |
| Add an endpoint, a setting, a column, a dashboard page; change the spec | [docs/development.md](docs/development.md) |
| Deploy, upgrade, back up, rotate the Claude token, troubleshoot | [docs/operations.md](docs/operations.md) |
| *Call* the API from an agent (consumer side) | [docs/agent-api-guide.md](docs/agent-api-guide.md), served at `/llms.txt` |
| See what isn't built yet | [docs/missing-features.md](docs/missing-features.md) |
| Know which choices are deliberate (don't "fix" them) | [docs/architecture.md → Design decisions](docs/architecture.md#design-decisions) |

## What this is

A FastAPI app that exposes an **OpenAI-compatible LLM API** whose completions come from **Claude Code's
headless mode** (`claude -p`), run as a subprocess inside the same container. It adds API-token auth,
server-side **sessions** (Claude Code `--session-id` / `--resume`), request logging, and a server-rendered
**admin dashboard** (`/admin`, including a chat). It ships as one container via `docker compose`.

Stack: Python (3.12 in the image, **3.10 locally**), FastAPI, SQLAlchemy 2 (sync) on SQLite, Jinja2
templates, vanilla JS and CSS (no build step), pytest.

## Rules — read before doing anything

1. **Never touch the operator's `.env`.** Don't delete, overwrite, `cp` over or "temporarily" replace it. It
   holds the real `CLAUDE_CODE_OAUTH_TOKEN`. If you need different env values, pass them on the command line
   or use `.claude/launch.json` (the `api-dev` server), which overrides `.env` without changing it.
2. **Never run `docker compose down -v`** or remove the `claude-headless-api_claude-api-data` volume. It holds
   the database (users, hashed API tokens, sessions, logs) and Claude's transcripts. Re-creating tokens is
   painful for the operator. Plain `docker compose up -d --build` keeps the volume.
3. **Don't start, stop or rebuild the operator's running stack** (`docker compose …`, port 8000) unless asked.
   Verify UI changes on the dev server (port 8010, its own `dev.db`) and clean up `dev.db*` and
   `.dev-claude/` afterwards.
4. **Tests must never call the real Claude CLI.** They use `tests/fake_claude.py` (wired through
   `CLAUDE_BIN`). Real calls cost money or quota and need credentials.
5. **The OpenAPI spec is part of the API.** Any change to a `/v1` route, its request or its response must
   update `docs/openapi.yaml`, then run `python scripts/export_openapi.py`. The tests fail on drift: an
   undocumented route, a stale `openapi.json`, an example or a live response that doesn't match its schema.
6. **One Uvicorn worker.** The rate limiter, the concurrency gate and the per-session locks live in process
   memory. Don't add `--workers` and don't add another in-memory state without noting it in
   docs/missing-features.md.
7. **Write Python 3.10-compatible code** (the local venv is 3.10): no `datetime.UTC`, `enum.StrEnum`,
   `typing.Self`, `except*` or `tomllib`.
8. **Secrets:**
   - API tokens are stored as SHA-256 hashes; the plaintext appears exactly once, in the creation response.
   - The admin session cookie is *signed, not encrypted*, so never put secrets in `request.session` or in
     flash messages.
   - Never log `CLAUDE_CODE_OAUTH_TOKEN`.
9. **Don't pass `--bare` to `claude`.** It ignores OAuth and only reads `ANTHROPIC_API_KEY`, which breaks
   subscription auth.
10. **Telemetry carries no content.** Never put prompts, system prompts, replies or tokens into span
    attributes, metric labels or log fields (`tests/test_telemetry.py` checks it). Record numbers and
    identifiers only, through the helpers in `app/telemetry.py`.
11. **Respect Anthropic's terms** (README → *Terms of use*). Don't build features that sell access, take
    payments for usage, or let a subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) serve users other than its
    owner. Multi-user deployments use `ANTHROPIC_API_KEY`. Don't present the project as Anthropic's.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q                                   # full suite, ~15s, no network, no Claude calls
.venv/bin/pytest -q tests/test_sessions.py -k resume  # one file / one test
.venv/bin/python scripts/export_openapi.py            # regenerate docs/openapi.json from the YAML
.venv/bin/python scripts/export_openapi.py --check    # exit 1 if the JSON is stale
```

Load testing (never against real Claude; the script aborts if the target isn't in test mode):

```bash
scripts/test_mode.sh --fresh                   # app on :8020 with the fake CLI (or launch.json "api-test-mode")
k6 run -e PROFILE=smoke loadtest/stress.js     # smoke | load | stress | spike | soak
```

Test-mode knobs: `FAKE_LATENCY_MS=800-2500`, `FAKE_ERROR_RATE=0`, `MAX_CONCURRENCY`, `PORT`.

Dev server (doesn't touch `.env` or Docker): the `api-dev` entry in `.claude/launch.json`, or

```bash
SECRET_KEY=dev ADMIN_USERNAME=admin ADMIN_PASSWORD=dev-password DATABASE_URL=sqlite:///./dev.db \
CLAUDE_DATA_DIR=./.dev-claude .venv/bin/uvicorn app.main:app --reload --port 8010
```

The dev server has no Claude login, so completions return `502 Not logged in`. To exercise the happy path
in a browser, add `CLAUDE_BIN=tests/fake_claude.py`.

Container (operator's deployment; see rule 3): `docker compose up -d --build`, logs with
`docker compose logs -f api`.

## Repository map

| Path | What lives there |
|---|---|
| `app/main.py` | App factory, lifespan (schema creation and additive upgrade, admin bootstrap, retention loop), exception handlers |
| `app/config.py` | Process settings from env / `.env` (`Settings`, cached by `get_settings()`) |
| `app/runtime_settings.py` | Admin-editable settings stored in the `app_settings` table, with defaults in `DEFAULTS` |
| `app/models.py` | SQLAlchemy models: `User`, `ApiToken`, `RequestLog`, `AppSetting`, `ChatSession`, `ChatMessage` |
| `app/db.py` | Engine and session factory (`init_engine`, `session_factory`, `get_db`), SQLite pragmas |
| `app/security.py` | Password hashing, token generation and hashing, `authenticate`, `APIError` (OpenAI error shape), admin session, CSRF |
| `app/claude_runner.py` | **The only place that runs `claude`**: argv, env allowlist, cwd, concurrency gate, timeout, output parsing, transcript files |
| `app/sessions.py` | Session lifecycle: create, `run_turn` (resume or rebuild), delete, retention, JSON shape |
| `app/openai_schemas.py` | Pydantic request models; messages → (system prompt, prompt) conversion; `chat.completion` response builder |
| `app/request_logs.py` | Fills `RequestLog` from a Claude result |
| `app/ratelimit.py` | In-memory sliding-window limiter per token |
| `app/telemetry.py` | OpenTelemetry setup (OTLP to Alloy, off by default) and the span, metric and log helpers |
| `app/routers/api.py` | `/v1/*` and `/healthz`. `_logged()` wraps every endpoint that runs Claude |
| `app/routers/admin.py` | Dashboard (`/admin/*`): HTML pages, POST-redirect-GET, `post()` = CSRF-protected POST route |
| `app/routers/docs.py`, `app/api_docs.py` | `/docs` reference page built from the spec; `/openapi.json`, `/openapi.yaml`, `/llms.txt` |
| `app/templates/` | Jinja templates. `_macros.html` has the shared tables and form fields; `_head.html` applies the theme before paint |
| `app/static/` | `app.css` (design tokens, light/dark), `app.js` (confirm, copy, theme), `chat.js`, `docs.js` |
| `docs/` | OpenAPI spec (YAML is the source, JSON generated) and the guides listed above |
| `scripts/export_openapi.py` | YAML → JSON for the spec |
| `scripts/test_mode.sh` | Runs the app with the fake CLI and its own state in `.test-mode/` |
| `loadtest/stress.js` | k6 load and stress test (creates and deletes its own user and tokens) |
| `grafana/` | Observability stack (compose and configs) and the provisioned dashboard. Dashboard queries use the Prometheus names of the metrics in `app/telemetry.py`: rename a metric or label there, update `grafana/dashboards/` too |
| `tests/` | pytest suite; `fake_claude.py` is the CLI stand-in |

## Conventions

- **Layering.** Routers stay thin and push logic into modules (`sessions.py`, `claude_runner.py`, …). Only
  `claude_runner.py` spawns the CLI.
- **API errors.** Raise `APIError(status, message, type, code)`; `_logged()` turns it into OpenAI's error
  JSON and a `RequestLog` row. Codes are documented in the `Error` schema of the spec, so add new ones there.
- **Every endpoint that runs Claude writes one `RequestLog` row**, including rejected and failed calls.
  Prompt and response bodies are stored only when the `log_bodies` setting is on. Session messages are
  always stored.
- **Time.** Datetimes are naive UTC (`models.utcnow()`). Convert to epoch seconds with
  `.replace(tzinfo=timezone.utc)`, never a bare `.timestamp()`.
- **Dashboard:**
  - Mutating routes use `@post(...)`, which enforces CSRF; forms include `{{ csrf_field() }}`.
  - Handlers redirect after POST with `flash()` + `back()`.
  - JSON endpoints used by `chat.js` send the CSRF token as a form field.
- **Front end.**
  - No frameworks or CDNs: the app must work offline and on-prem.
  - Colours come only from the CSS variables in `app.css`. Dark mode is defined twice, under
    `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` and `:root[data-theme="dark"]`,
    so keep both blocks in sync.
  - Static files are cached by browsers: hard-reload after CSS/JS edits.
- **Style.** Ruff config in `pyproject.toml` (line length 120). Match the surrounding code's comment density;
  comments explain *why*.
- **Schema changes.** Tables come from the models. On startup, `create_schema()` adds new **nullable**
  columns to existing tables. Anything else (rename, type change, NOT NULL without default) needs a manual
  migration; see docs/development.md.

## Testing

Fixtures in `tests/conftest.py`:

- `client`: an app on a fresh temp SQLite database and temp Claude data dir.
- `db`: a session on that database.
- `make_token(username, **token_fields)`: returns `(raw_token, ApiToken)`.
- `admin_client`: a client logged in as the bootstrap admin.
- `client_csrf(client, path)`: scrapes a CSRF token from a page.

The fake CLI (`tests/fake_claude.py`):

- Echoes `echo[<model>|<effort>]<#turn>: <prompt>`.
- Emulates Claude Code session files under `CLAUDE_CONFIG_DIR/projects/<cwd>/`.
- Speaks `--output-format stream-json` like the real CLI (requires `--verbose`; with
  `--include-partial-messages` it streams a thinking block, then the text in pieces).
- Switches: `FAKE_CLAUDE_MODE=ok|error|crash|sleep|fail_midway`, `FAKE_CLAUDE_DELTA_DELAY_MS=<ms>` (pause between
  streamed pieces) and `FAKE_CLAUDE_ARGS_FILE=<path>`, which dumps the argv,
  stdin, cwd and env names it received. Use it to assert on flags.

The runner forwards only allow-listed env vars to the CLI; conftest adds the `FAKE_CLAUDE_*` switches to that
list. Tests set their own env, so they don't depend on a developer's `.env`.

## Definition of done

- [ ] `.venv/bin/pytest -q` passes. New behaviour has a test, using the fake CLI for anything that runs Claude.
- [ ] `/v1` changes are reflected in `docs/openapi.yaml` and the regenerated `docs/openapi.json`, including
      description, examples and `x-codeSamples`.
- [ ] UI changes were checked on the dev server in light **and** dark theme, and at narrow width.
- [ ] README, docs/missing-features.md and these agent docs still tell the truth.
- [ ] `.env`, the Docker volume and the operator's running container were left alone.
