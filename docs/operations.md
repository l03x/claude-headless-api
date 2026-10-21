# Operations runbook

How to run, upgrade, back up and troubleshoot a deployment. Commands are run from the repository root on the
Docker host. The service is called `api` in `docker-compose.yml` and its container is `claude-headless-api`.

> **Agents:** everything here acts on the operator's live deployment. Only run these commands when the
> operator asks you to, and never run the ones under [Destructive commands](#destructive-commands) unless
> explicitly told to, by name.

## What holds state

| Thing | Where | Lost if… |
|---|---|---|
| Secrets and config (`CLAUDE_CODE_OAUTH_TOKEN`, `SECRET_KEY`, `ADMIN_*`, `PORT`, …) | `.env` next to `docker-compose.yml` | the file is deleted or overwritten |
| Users, **hashed API tokens**, sessions, messages, logs, settings | `/data/app.db` in volume `claude-headless-api_claude-api-data` | the volume is removed (`down -v`) |
| Claude Code config and session transcripts | `/data/claude/` in the same volume | same |

If the database is lost, every API token has to be re-issued to its users. Back it up (below).

## First deployment

```bash
cp .env.example .env        # then fill it in: see the README's variable table
docker compose up -d --build
docker compose ps           # wait for "healthy"
```

Open `http://<host>:<PORT>/admin`, sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`, then go to
**System → Test Claude**. A reply means the CLI is logged in.

## Everyday commands

```bash
docker compose ps                   # status and health
docker compose logs -f --tail 100 api
docker compose restart api          # restart, same image and same env (does NOT re-read .env)
docker compose up -d                # apply .env changes (recreates the container, keeps the volume)
docker compose up -d --build        # deploy new code (rebuilds the image, keeps .env and the volume)
curl -fsS http://localhost:8000/healthz
```

## Rotating the Claude token

Symptoms: every completion fails with `502 … Not logged in` or `OAuth session expired`.

1. On a machine where you are signed in to Claude, run `claude setup-token` and copy the token.
2. Edit **only** the `CLAUDE_CODE_OAUTH_TOKEN=` line in `.env`.
3. Run `docker compose up -d`. `restart` is not enough, because it keeps the old environment.
4. Check **System → Test Claude** in the dashboard.

API tokens, users and sessions are unaffected.

## Upgrading

**Application code:**

```bash
git pull
docker compose up -d --build
```

The schema is upgraded automatically for added tables and nullable columns. Read the release notes for any
manual migration first.

**Claude Code version:** set `CLAUDE_CODE_VERSION=x.y.z` in `.env` (it is a build argument), then
`docker compose up -d --build`. Afterwards check the version on the System page and run Test Claude. Auto-update
is disabled inside the container on purpose, so the version only changes when you rebuild.

## Backups

SQLite runs in WAL mode, so don't copy `app.db` while the app is running. Use SQLite's backup API:

```bash
# 1. consistent snapshot inside the container
docker compose exec -T api python -c "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
# 2. copy it (and the Claude transcripts) out
docker compose cp api:/data/backup.db ./backup-$(date +%F).db
docker compose cp api:/data/claude ./claude-backup-$(date +%F)
# 3. tidy up
docker compose exec -T api rm /data/backup.db
```

Store backups somewhere other than the Docker host. Without the transcripts, sessions still work: their next
turn is rebuilt from the database.

## Restoring

Restore into the live database with the same backup API (safe in WAL mode), then restart:

```bash
docker compose cp ./backup-YYYY-MM-DD.db api:/data/restore.db
docker compose exec -T api python -c "import sqlite3; s=sqlite3.connect('/data/restore.db'); d=sqlite3.connect('/data/app.db'); s.backup(d); d.close()"
docker compose exec -T api rm /data/restore.db
docker compose restart api
```

To restore transcripts, `docker compose cp ./claude-backup-YYYY-MM-DD/. api:/data/claude/`.

## Resetting an admin password

If another admin can sign in, use **Users → (user) → Dashboard password**. Otherwise run the following; the
password is read without echo and stays out of shell history:

```bash
read -rsp "New password: " NEW_PW; echo
docker compose exec -T -e NEW_PW="$NEW_PW" api python -c "import os; from app.db import session_factory; from app.models import User; from app.security import hash_password; db=session_factory(); u=db.query(User).filter_by(username='admin').one(); u.password_hash=hash_password(os.environ['NEW_PW']); u.is_active=True; db.commit(); print('ok')"
unset NEW_PW
```

Replace `'admin'` with the username. `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` are used **only** when no
admin exists at all, so changing them does not reset an existing password.

## Tuning

All of these are on the dashboard's **Settings** page and take effect immediately:

| Symptom | Setting |
|---|---|
| Requests queue up (System page shows "queued") | Raise *Max concurrent runs*. Each run is a separate `claude` process, so watch the host's memory (`docker stats`) |
| `504 timeout` on hard prompts | Raise *Request timeout*, or ask clients to use a lower effort |
| Legitimate clients hit `429` | Raise the token's rate limit (token page) or the global default |
| Database growing | Lower *Log retention*; set *Session retention*; keep *Log bodies* off |
| Runaway cost per request | Set *Budget cap per request* |

## Observability (OpenTelemetry → Grafana Alloy)

The app can send **traces, metrics and logs** over OTLP. It is off until `OTEL_EXPORTER_OTLP_ENDPOINT` is
set; add the variables to `.env`, then run `docker compose up -d`:

| Variable | Default | |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (off) | Alloy's OTLP receiver: `http://alloy:4318` (HTTP) or `http://alloy:4317` (gRPC) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | or `grpc` |
| `OTEL_EXPORTER_OTLP_HEADERS` | — | e.g. `authorization=Basic …` when the receiver needs auth |
| `OTEL_SERVICE_NAME` | `claude-headless-api` | |
| `OTEL_RESOURCE_ATTRIBUTES` | — | e.g. `deployment.environment=prod` |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | always on | e.g. `parentbased_traceidratio` and `0.1` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` ms | |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp` | `none` turns that signal off |
| `OTEL_SDK_DISABLED` | `false` | `true` turns everything off |

**Alloy:** [`docs/examples/alloy.config.alloy`](examples/alloy.config.alloy) receives OTLP on 4317/4318 and sends
traces to Tempo, metrics to Prometheus (remote write) and logs to Loki. It was checked to load on Alloy v1.21.

**Networking:** the container must reach Alloy.
- If Alloy runs in Docker on the same host, attach both to one network (for example an external network
  added to `docker-compose.yml`) and use its service name.
- If Alloy runs on the host itself, use `http://host.docker.internal:4318` together with
  `extra_hosts: ["host.docker.internal:host-gateway"]`.

What is sent:

| Signal | Contents |
|---|---|
| Traces | One trace per request: the HTTP server span, SQL spans (statements only, never values), and `chat <model>` (GenAI conventions: model, effort, session mode, token usage, cost, outcome) with child spans `claude.queue_wait` and `claude.process`. Health checks and static files are not traced |
| Metrics | `claude.runs` (by model, effort, session mode, outcome), `claude.run.duration`, `claude.queue.wait`, `claude.tokens` (by type), `claude.cost`, gauges `claude.in_flight` / `claude.queued` / `claude.concurrency_limit`, `api.requests` (by endpoint, status, error code), plus HTTP-server and DB-connection metrics |
| Logs | One record per API or dashboard-chat request (endpoint, status, model, user, token prefix, latency), plus the server's own log lines, each carrying the trace ID |

- **Prometheus names.** Alloy converts names: dots become underscores, units become suffixes and counters
  get `_total`. For example, `claude.runs` → `claude_runs_total` and `claude.run.duration` →
  `claude_run_duration_seconds`.
- **Finding a request's trace.** Each request-log row in the dashboard shows its **Trace ID**; search for it
  in Tempo.
- **Privacy.** Prompts, system prompts and replies are never put in spans, metric labels or logs.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `502 … Not logged in · Please run /login` | `CLAUDE_CODE_OAUTH_TOKEN` missing or not applied | Set it in `.env`, then `docker compose up -d` |
| `502 … OAuth session expired` / `Failed to authenticate` | Token revoked or expired | [Rotate the Claude token](#rotating-the-claude-token) |
| `502` mentioning the model | Model id not known to the pinned CLI | Fix the enabled models in Settings, or upgrade Claude Code |
| `500 Claude CLI not found` | Broken image or a wrong `CLAUDE_BIN` | Rebuild: `docker compose up -d --build` |
| Container exits: `Set SECRET_KEY…` | `SECRET_KEY` empty or the placeholder | Set a random value in `.env` |
| Container exits: `No admin user exists…` | Fresh database and no `ADMIN_USERNAME`/`ADMIN_PASSWORD` | Set both in `.env` |
| Login form keeps returning to the login page | `SESSION_COOKIE_SECURE=true` on plain HTTP, or the browser blocks cookies | Use HTTPS, or set it to `false` |
| "CSRF check failed" | The dashboard tab outlived its session (12 h) or the server restarted with a new `SECRET_KEY` | Reload and sign in again |
| `database is locked` in logs | Several processes writing to the same `app.db` | Run exactly one container with one worker |
| A session "forgot" earlier turns | Transcript missing; it was rebuilt from stored messages | Check that `/data/claude` is on the volume; restore transcripts from backup |
| Port already in use on `up` | Another service holds `PORT` | Change `PORT` in `.env` |
| Stale styles after an upgrade | Browser cache | Hard-reload (Ctrl/Cmd + Shift + R) |
| Nothing reaches Alloy | Endpoint unset or unreachable from the container | Check the startup log for `OpenTelemetry export enabled → …`; then test the port from inside the container: `docker compose exec api python -c "import socket; socket.create_connection(('alloy', 4318), 3); print('reachable')"` |

Useful views: the dashboard **Logs** page (filter by status `error`), each log's detail page (full error
text), and **System** (CLI version, token present, queue).

## Destructive commands

Never run these casually. Each one destroys something that can't be recovered without a backup.

| Command | Destroys |
|---|---|
| `docker compose down -v` | The data volume: users, **API tokens**, sessions, logs, transcripts |
| `docker volume rm claude-headless-api_claude-api-data` | Same |
| `rm .env`, `cp something .env`, `> .env` | The Claude token and secrets (and with a new `SECRET_KEY`, every admin is signed out) |
| `docker compose exec api rm /data/app.db` | The database |
| Dashboard: *Logs → Purge* with 0 days | Every request log |

`docker compose down` **without** `-v` is safe: it removes the container and network but keeps the volume.
