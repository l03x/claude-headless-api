# Architecture

How Claude Headless API is put together and why. For day-to-day rules see [AGENTS.md](../AGENTS.md).

## Design decisions

Deliberate choices. They can look like gaps, so don't "fix" them without discussing the trade-off first.

| Decision | Why |
|---|---|
| Claude runs with no tools (`--tools ""`), empty settings and no MCP servers | It's an LLM API, not an agent host. Claude can't run commands or read files on the server, and every caller gets the same behaviour regardless of what is on disk |
| The one exception is opt-in **WebSearch**, off unless an admin allows it and the request sends `web_search_options`. WebFetch is never enabled | WebSearch runs on Anthropic's side and reaches nothing on this host's network. WebFetch downloads URLs from inside the container, so a prompt could point it at the dashboard, other services on the Docker network or cloud metadata endpoints |
| Unknown OpenAI parameters (`temperature`, `max_tokens`, …) are accepted and ignored, not rejected | Existing OpenAI clients send them by default. Rejecting them would break drop-in compatibility for settings the CLI can't honour anyway |
| The Claude Code version is pinned in the image (`CLAUDE_CODE_VERSION`), auto-update is off | A CLI update can change flags or output. Upgrades happen on rebuild, after a check |
| No `--bare` flag | It disables OAuth, which `CLAUDE_CODE_OAUTH_TOKEN` needs |
| One container, one Uvicorn worker, SQLite on a volume | Simple to run on-prem with one `docker compose up`. The rate limiter, concurrency gate and session locks are in process memory because of it (scaling out is in missing-features.md) |
| Plain HTTP inside the container | TLS belongs to a reverse proxy in front (Caddy, Traefik, nginx), with `SESSION_COOKIE_SECURE=true` |
| Prompts and replies of stateless requests are not stored unless *Log bodies* is on; session messages always are | Privacy by default; a session is useless without its history |
| Telemetry is off by default and never carries message content | Opt-in export, and observability data must not become a copy of users' conversations |
| The OpenAPI spec is hand-written (`docs/openapi.yaml`); FastAPI's generated spec and Swagger UI are off | Endpoints parse bodies themselves, so the generated spec would be incomplete. The hand-written one is tested against real responses |
| The dashboard UI uses no frameworks or CDNs | It must work offline and on-prem with no build step |

## Big picture

```
                    ┌──────────────────────── container: claude-headless-api ───────────────────────┐
 OpenAI SDK / curl  │  FastAPI (1 Uvicorn worker)                                                  │
 ──── /v1/* ──────► │   routers/api.py ── auth, limits, logging (_logged)                          │
                    │        │                                                                     │
 Browser            │        ├──► sessions.py ── per-session lock, resume or rebuild               │
 ──── /admin/* ───► │   routers/admin.py        │                                                  │
 ──── /docs ──────► │   routers/docs.py         ▼                                                  │
                    │                   claude_runner.py ── concurrency gate ── spawn `claude -p`  │
                    │                           │                     (one process per request)    │
                    │   SQLite /data/app.db ◄───┘                      │                           │
                    │   Claude state /data/claude/{config,sessions} ◄──┘                           │
                    └──────────────────────────────────────────────────────────────────────────────┘
                                             │ HTTPS (CLAUDE_CODE_OAUTH_TOKEN)
                                             ▼
                                        Anthropic API
```

Everything durable lives in one Docker volume mounted at `/data`:

| Path | Contents |
|---|---|
| `/data/app.db` (+ `-wal`, `-shm`) | SQLite: users, token hashes, sessions and their messages, request logs, runtime settings |
| `/data/claude/config/` | `CLAUDE_CONFIG_DIR` for every CLI run: Claude Code's config and **session transcripts** (`projects/<cwd>/<uuid>.jsonl`) |
| `/data/claude/sessions/` | The fixed working directory that session turns run in (transcripts are filed by cwd) |

## Request lifecycle: stateless chat completion

`POST /v1/chat/completions` in `app/routers/api.py`:

1. **`_logged()`** starts a `RequestLog` row (status `error` until proven otherwise) and loads the runtime
   settings.
2. **`authenticate()`** (`security.py`) hashes the presented token, looks it up, and rejects it if it is
   missing, unknown, revoked, expired or its user is disabled. It also updates `last_used_at`.
3. **`_parse()`** validates the body with `ChatCompletionRequest`. Unknown OpenAI fields are allowed and
   ignored; `reasoning_effort` is validated separately so that `none`/`minimal` can map to `low`.
4. **Checks.** `_check_access()` covers enabled model, token allow-list and effort cap;
   `_check_rate_limit()` applies the per-token requests/minute limit.
5. **`to_prompt()`** (`openai_schemas.py`):
   - `system`/`developer` messages become the system prompt, falling back to the `default_system_prompt`
     setting. A system prompt is always passed, which replaces Claude Code's agentic default.
   - Earlier turns become a `<conversation>` transcript ahead of the last user message.
6. **`run_claude()`** (`claude_runner.py`) waits for a slot in the **concurrency gate**, then spawns the CLI
   (below) in a fresh temp directory and parses its JSON output.
7. The result is recorded on the log row (`record_result`) and returned as a `chat.completion`.
8. `_logged()` always writes the log row, whatever happened: success, `APIError` (4xx, status `rejected`)
   or `ClaudeError` (5xx, status `error`).

### How the CLI is invoked

```
claude -p --output-format json --model <m> --effort <e> --tools "" --system-prompt <s>
       --setting-sources "" --strict-mcp-config --disable-slash-commands
       [--tools WebSearch --allowedTools WebSearch --max-turns 10   (instead of --tools "", web search only)]
       [--no-session-persistence | --session-id <uuid> | --resume <uuid>]
       [--json-schema <schema>] [--max-budget-usd <n>]
```

| Flag | Why |
|---|---|
| `-p --output-format json` | Headless, one JSON result with `result`, `usage`, `total_cost_usd`, `session_id`, `is_error` |
| `--tools ""` | No tools: Claude can't run commands or touch files. It is a pure text model |
| `--tools WebSearch --allowedTools WebSearch --max-turns 10` | Web search requests only. Print mode has nobody to answer a permission prompt, so the tool is pre-approved. Each search costs a model turn, and `--max-turns` (`WEB_SEARCH_MAX_TURNS`) bounds a run that keeps searching |
| `--system-prompt` | Replaces the coding-agent system prompt with the caller's (or the default) |
| `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands` | Ignore any settings, MCP servers or skills found on disk |
| `--no-session-persistence` | Stateless calls leave nothing on disk |
| `--output-format stream-json --verbose --include-partial-messages` | Streamed replies only. The CLI requires `--verbose` with stream-json in print mode; partial messages add the token-level deltas |
| `--session-id` / `--resume` | Sessions (see below) |
| *not* `--bare` | `--bare` ignores OAuth, which would break `CLAUDE_CODE_OAUTH_TOKEN` |

Process hygiene in `run_claude()`:

- The prompt goes on **stdin** (no argv length limits, no shell).
- The child gets an **allow-listed environment** (`_ENV_PASSTHROUGH` plus `CLAUDE_CONFIG_DIR`,
  `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`). Server secrets such as `SECRET_KEY`
  never reach it.
- It runs in its own process group. On timeout the whole group is killed and the API returns `504`.
- A non-zero exit or `is_error: true` becomes a `ClaudeError` (`502`) carrying the CLI's message.

### Streaming

`stream: true` (chat completions, session messages, and the dashboard chat with `stream=1`) uses
`stream_claude()` instead of `run_claude()`:

- **Reading the CLI.** It reads the CLI's stdout line by line and yields only `text_delta`s, never thinking
  deltas, then one `ClaudeResult` built from the final `result` line.
  - With `json_schema`, deltas are skipped and the finished `structured_output` is sent as one piece.
  - If no deltas arrive, the `result` text is sent as one piece.
  - The stdout reader's line limit is 16 MiB, because the `result` line holds the whole reply.
  - stderr is drained alongside so the pipe can't fill, and one deadline covers the whole run.
- **Priming.** Before responding, the handler pulls the first item. Anything that fails before text exists
  (credentials, model, timeout while queued) is still a normal JSON error with its status code. After that the
  response is `text/event-stream` with OpenAI `chat.completion.chunk`s; a later failure is sent as one
  `data: {"error": …}` event.
- **Logging.** `_logged()` gets an `SSEStream` back and writes the request-log row when the stream ends,
  through a fresh DB session, since the request's own is gone by then. Streamed session turns also get their
  own DB session.
- **Disconnects.** The client going away closes the generators. The CLI's process group is killed, the
  log row gets `http_status=499`, and a session turn stores nothing. It also drops its Claude transcript,
  which may hold half a turn, so the next turn rebuilds from the stored messages.
- **Cancellation safety.** Starlette cancels a stream through anyio, which re-raises at every `await`
  during cleanup. So cleanup does its synchronous work first (kill, bookkeeping), shields the `aclose()` calls,
  and the concurrency gate releases its slot without awaiting. An awaiting release would leak a slot on every
  disconnect.
- **Several turns.** With web search, Claude often writes a sentence, calls the tool, then answers in a
  new model turn. Text from every top-level turn is streamed, with a blank line between turns. The CLI's
  `result` holds only the last turn, so when turns were joined the streamed text replaces it in the
  `ClaudeResult`: a stored session message reads the same after a reload as it did while streaming.
  Non-streamed replies carry only the final answer.
- **Telemetry.** Spans are started without being made current (`telemetry.start_claude_span`), because the
  generator resumes in more than one task. `claude.time_to_first_token` records CLI start → first delta.

## Sessions

Implemented in `app/sessions.py`; API in `routers/api.py`, dashboard chat in `routers/admin.py`.

- A `ChatSession` row has our public `id` and a separate `claude_session_id`, the transcript it resumes.
- **First turn:** generate a UUID and run with `--session-id <uuid>` in `/data/claude/sessions`.
  **Later turns:** run with `--resume <claude_session_id>` and send only the new message.
- **Rebuild.** Before resuming, `transcript_paths()` checks that the `.jsonl` exists. If it is gone (volume
  restored without transcripts, manual cleanup), the turn starts a *new* Claude session whose prompt is the
  stored history rendered as a transcript, and `claude_session_id` is replaced.
  A turn without web search also rebuilds when `transcript_web_search` says the transcript holds WebSearch
  calls: the API rejects tool calls in a request that defines no tools. Our stored copy has only the text.
- **One turn at a time per session**, enforced by an `asyncio.Lock` per session id in process memory.
- **Storage.** Both sides of each successful turn are stored as `ChatMessage` rows (for `GET
  /v1/sessions/{id}` and the dashboard), together with per-session token and cost totals. A failed turn
  stores nothing. A session created implicitly (`session_id: "new"`, or the dashboard's first message) is
  deleted again if its first turn fails.
- **Ownership** is per user, not per token. Lookups that don't match the caller's user return `404`, not
  `403`, so ids can't be probed.
- **Deletion** removes the DB rows and the transcript file. Deleting a user removes their transcripts first;
  the DB rows go by cascade.
- **Retention.** `session_retention_days` (0 = forever) is applied by the background loop in `main.py`
  every 6 hours, together with `log_retention_days`.

## Data model (`app/models.py`)

| Table | Key columns | Notes |
|---|---|---|
| `users` | `username` (unique), `is_admin`, `is_active`, `password_hash` | Only admins have passwords and log in. Everyone else is an API consumer |
| `api_tokens` | `token_hash` (SHA-256, unique), `prefix`, `expires_at`, `revoked_at`, `allowed_models` (JSON), `rate_limit_rpm`, `max_effort` | `status()` returns `active`, `revoked` or `expired` |
| `request_logs` | `status` (`ok`, `error`, `rejected`), `http_status`, `endpoint`, model/effort, tokens, `cost_usd`, `latency_ms`, `chat_session_id`, `prompt`/`response` (only with `log_bodies`) | Plain integer `user_id`/`token_id` (no FK), so logs survive deletions |
| `chat_sessions` | `id` (UUID), `user_id` (FK, cascade), `source` (`api`/`dashboard`), `claude_session_id`, `system_prompt`, totals | |
| `chat_messages` | `session_id` (FK, cascade), `role`, `content`, per-turn usage | |
| `app_settings` | `key`, `value` (JSON) | Overrides `runtime_settings.DEFAULTS` |

**Schema evolution.** `create_schema()` runs `create_all` and then adds any **nullable** column that the
models have and the table lacks (`ALTER TABLE … ADD COLUMN`). Other changes are manual (see
[development.md](development.md#changing-the-database-schema)).

## Two kinds of configuration

| | Process settings (`app/config.py`) | Runtime settings (`app/runtime_settings.py`) |
|---|---|---|
| Source | Environment / `.env`, read once (`lru_cache`) | `app_settings` table, read per request |
| Changed by | Editing `.env`, then recreating the container | The dashboard's Settings page |
| Examples | `SECRET_KEY`, `DATABASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_BIN`, `CLAUDE_DATA_DIR`, `CLAUDE_ENV_PASSTHROUGH`, `ADMIN_*`, `SESSION_COOKIE_SECURE` | enabled models, defaults, timeout, concurrency, rate limit, body logging, retention, default system prompt, budget cap |

## Concurrency and limits

- **Concurrency gate** (`ConcurrencyGate` in `claude_runner.py`): at most `max_concurrency` CLI processes
  at once; the rest wait, with no strict ordering among them. It is resized from settings on each request, and `in_flight` /
  `queued` are shown on the Overview and System pages.
- **Rate limiter** (`ratelimit.py`): a 60-second sliding window per token id. 0 means unlimited. The token's
  own limit wins over the global default.
- **Timeout:** `request_timeout_s` covers the CLI run only, not time spent queued at the gate.
- **No DB connection while waiting.** Requests end their read transaction (`release_connection()`) before
  awaiting Claude. Otherwise queued requests would hold all of SQLAlchemy's pooled connections, and the next
  request would block the event loop waiting for one, stalling the whole server. The k6 spike test
  (`loadtest/stress.js`) catches this.
- All three live in memory, which is why the app must run **one worker** (see missing-features.md for the
  Redis path).

## Security model

- **API consumers.**
  - Bearer token (or `x-api-key`): random 32 bytes, `chk_` prefix, stored as SHA-256.
  - The token's restrictions (allowed models, maximum effort, rate limit, expiry) are enforced before Claude
    runs.
- **Admins.**
  - Username and password (argon2) log in to `/admin`.
  - The session is Starlette's signed cookie (`SameSite=Strict`, 12 h, `Secure` when
    `SESSION_COOKIE_SECURE=true`).
  - Every mutating dashboard route checks a CSRF token stored in the session.
  - `next` redirects are restricted to `/admin…` paths.
- **Bootstrap.** At startup, if no admin user exists at all, one is created from `ADMIN_USERNAME` /
  `ADMIN_PASSWORD` (an existing user with that name is promoted). Startup fails if neither an admin nor
  these variables exist, and it also refuses the placeholder `SECRET_KEY`.
- **Isolation of the CLI:** no tools (WebSearch only when allowed and asked for), env allowlist, empty
  settings and MCP, its own process group.
- **Not covered yet:** login brute-force protection, 2FA, audit log, TLS (use a reverse proxy). See
  [missing-features.md](missing-features.md).

## Observability

`app/telemetry.py` sets up OpenTelemetry when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (standard `OTEL_*`
variables; see [operations.md](operations.md#observability-opentelemetry--grafana-alloy)). Otherwise it is
a no-op, and the helpers it exposes (`claude_span`, `record_run`, `record_request`) cost nothing.

- **Traces.**
  - FastAPI and SQLAlchemy are auto-instrumented, but SQL statements carry no bound values.
  - `run_claude()` opens `chat <model>`, with children `claude.queue_wait` (time at the concurrency gate)
    and `claude.process` (the CLI run).
  - A filtering span processor drops SQLAlchemy `connect` spans and SQL spans with no parent request.
- **Metrics:** custom instruments are recorded in `run_claude()` and in `_logged()` / dashboard chat.
  Observable gauges read the concurrency gate.
- **Logs:** one `claude_api.requests` record per request, plus the server's own log lines, each carrying the
  trace context. `RequestLog.trace_id` stores the trace ID for the dashboard.
- **Content never leaves:** no prompt, system prompt or reply goes into telemetry, and
  `tests/test_telemetry.py` asserts it.

## Documentation pipeline

```
docs/openapi.yaml ──(scripts/export_openapi.py)──► docs/openapi.json ──► /openapi.json
        │                                                  │
        └──────────────────────────► /openapi.yaml         └──► app/api_docs.py ──► /docs (api_docs.html)
docs/agent-api-guide.md ──────────────────────────────────────────────────────────► /llms.txt
```

`/docs` renders the reference (parameters, field tables, examples, `x-codeSamples`) straight from the spec,
so the page can't contradict it. `tests/test_docs.py` keeps the spec honest: it must be valid OpenAPI 3.1,
the JSON must match the YAML, every served route must be documented and vice versa, every example must
match its schema, and real responses must match the documented schemas.
