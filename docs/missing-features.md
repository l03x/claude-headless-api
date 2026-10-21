# Missing features and known limitations

What Claude Headless API does **not** do yet, and what adding it would take. Items are grouped by area.
Within each group the most useful ones come first. Choices that are deliberate rather than missing are listed in
[architecture.md → Design decisions](architecture.md#design-decisions).

Legend: **Not supported** = rejected or ignored today · **Partial** = works with caveats.

---

## API compatibility

| Feature | Status | Today | What adding it would take |
|---|---|---|---|
| Streaming (`stream: true`, SSE) | Not supported | Rejected with `400 stream_unsupported`; the full reply arrives at once. | Run `claude -p --output-format stream-json --include-partial-messages` and translate its events into OpenAI `chat.completion.chunk` server-sent events. The request log and session messages would be written when the stream ends. |
| Sampling controls: `temperature`, `top_p`, `max_tokens`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `logit_bias` | Not supported | Accepted and silently ignored, because `claude -p` has no flags for them. | Not possible through the CLI. `max_tokens` could be approximated by asking for brevity in the system prompt, or enforced by truncating, but neither is exact. |
| `n > 1` (several choices) | Not supported | Ignored; there is always exactly one choice. | Run the CLI `n` times in parallel and merge the results. That costs `n` times as much and uses `n` concurrency slots. |
| `logprobs` | Not supported | Always `null`. | Not available from Claude Code. |
| `finish_reason` other than `stop` | Not supported | Always `stop`, even if a budget cap cut the run short. | Map the CLI's `stop_reason` / `terminal_reason` (`max_tokens`, budget exceeded, …) to `length` and similar values. |
| Image, audio and file input | Not supported | Non-text content parts are rejected with `400`. | Save attachments to a per-request directory and reference them in the prompt, enabling the `Read` tool for that directory only. This needs care so one user cannot read another's files. |
| Tool / function calling (`tools`, `tool_choice`, `tool` messages) | Not supported | Claude runs with `--tools ""`; `tool`/`function` messages are rejected. | Translate OpenAI tool definitions into an MCP server passed with `--mcp-config` that hands calls back to the client. That is a large change, because the client must take part in the loop. |
| `response_format: json_object` | Partial | Only adds "respond with a single JSON object" to the system prompt; the output is not validated. | Validate the reply and retry once if it is not valid JSON, or route it through `--json-schema` with `{"type": "object"}`. |
| Anthropic Messages API (`POST /v1/messages`) | Not supported | Only the OpenAI shape is offered. | Add a second router that maps the Messages format onto the same runner; most of the logic (auth, logging, sessions) is shared. |
| Stateless multi-turn history | Partial | Earlier turns are flattened into a tagged transcript inside one prompt, not sent as real conversation turns. | Use sessions for true multi-turn. For stateless calls, write a temporary Claude Code transcript and `--resume` it. |
| Token usage | Partial | `prompt_tokens` includes Claude Code's own system prompt and context, so it is larger than the text sent. | Inherent to the CLI. Document per-model baselines, or subtract a measured baseline. |
| Cost figures | Partial | `cost_usd` is what Claude Code reports. With a subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) nothing is billed per token, so it is an API-price estimate. | Label it as an estimate in the dashboard, or track subscription usage limits instead. |

## Sessions

| Feature | Status | Today | What adding it would take |
|---|---|---|---|
| Edit, regenerate or delete a single message | Not supported | Messages are append-only. | Fork the Claude session (`--resume <id> --fork-session`) at the edited turn and keep a parent pointer per message. |
| Branching / forking a conversation | Not supported | — | Same mechanism as above, exposed as `POST /v1/sessions/{id}/fork`. |
| Changing the system prompt mid-session | Not supported | Fixed when the session is created. | Start a new Claude session rebuilt from the stored history with the new prompt. The rebuild path already exists. |
| Export / import of conversations | Not supported | Read-only via `GET /v1/sessions/{id}` and the dashboard. | Add JSON / Markdown export to the dashboard and API. |
| Full-text search over messages | Not supported | The dashboard searches titles only. | SQLite FTS5 table over `chat_messages`. |
| Per-session cost or turn limits | Not supported | Only the global per-request budget cap applies. | Add `max_turns` / `max_cost_usd` columns, checked before each turn. |

## Scaling and storage

| Feature | Status | Today | What adding it would take |
|---|---|---|---|
| More than one worker or replica | Not supported | Must run a **single** Uvicorn worker: the rate limiter, the concurrency gate and the per-session locks live in process memory. | Move those to Redis (or database row locks), then run several workers or containers behind a load balancer. Claude transcripts would need shared storage. |
| PostgreSQL / other databases | Not supported | Only SQLite is used and tested, although `DATABASE_URL` is configurable and the code goes through SQLAlchemy. | Add a `psycopg` driver, make the schema upgrade work on PostgreSQL, and run the suite against it. |
| Schema migrations | Partial | New nullable columns are added automatically at startup; renames, type changes and drops are not. | Adopt Alembic with versioned migrations. |
| Horizontal session storage | Not supported | Transcripts are files under `/data/claude` on one volume. | Shared volume (NFS) or object-storage sync, plus the locking above. |

## Security and administration

| Feature | Status | Today | What adding it would take |
|---|---|---|---|
| Brute-force protection on the dashboard login | Not supported | Unlimited password attempts. | Per-IP and per-username attempt limits with back-off, and a log of failed logins. |
| Two-factor authentication / SSO for admins | Not supported | Username and password only. | TOTP enrolment, or OIDC login (for example through Keycloak). |
| Roles beyond "admin" | Not supported | Every dashboard user can do everything. | Add read-only "auditor" and "operator" roles checked in `require_admin`. |
| Audit log of admin actions | Not supported | Changes to users, tokens and settings are not recorded. | An `audit_events` table written by every dashboard POST, with its own page. |
| Self-service portal for API users | Not supported | API users cannot log in; only admins create, rotate or revoke tokens. | A user area to see one's own usage, sessions and tokens and to rotate them. |
| Usage quotas and spend budgets | Not supported | Only requests per minute per token and a per-request budget cap. | Daily or monthly request / token / cost quotas per user or token, checked before each run. |
| Token rotation with overlap | Not supported | Revoke and create a new token. | A "rotate" action that issues a new token and keeps the old one valid for a grace period. |

## Operations

| Feature | Status | Today | What adding it would take |
|---|---|---|---|
| Prometheus `/metrics` endpoint (pull) | Not supported | Metrics are only pushed over OTLP, so a collector such as Alloy is required. | Expose the OTel meter through a Prometheus exporter at `/metrics` for setups that scrape instead of receiving pushes. |
| Alerting | Not supported | Metrics exist, but no rules ship with the project. | Example alert rules: error rate, queue depth, `claude.runs{outcome="error"}` spikes (for example an expired OAuth token). |
| Automated backups | Not supported | A manual backup and restore procedure is in [operations.md](operations.md#backups); nothing runs on a schedule. | A scheduled SQLite backup plus a copy of `/data/claude`, with rotation. |
