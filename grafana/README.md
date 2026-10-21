# Observability stack

Grafana Alloy → Prometheus (metrics), Loki (logs), Tempo (traces) → Grafana, with the data sources
and a **Claude Headless API** dashboard provisioned.

```bash
docker network create claude-observability   # once; the app and this stack both join it
cd grafana
cp .env.example .env        # set GRAFANA_ADMIN_PASSWORD
docker compose up -d        # Grafana on http://localhost:3001
```

The app's `docker-compose.yml` always joins the same network, so its normal deploy (`docker compose up -d
--build`) keeps it connected. Turn export on by adding `OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4318` to the
app's `.env`. The network is external, so `docker compose down` in either folder never deletes it.

| Path | |
|---|---|
| `docker-compose.yml` | The stack. Only Grafana (`127.0.0.1:3001`) and Alloy (OTLP `4317`/`4318`, UI `12345`) are published |
| `alloy/config.alloy` | OTLP receiver → Tempo, Prometheus (remote write), Loki (OTLP) |
| `prometheus/`, `loki/`, `tempo/` | Single-node configs. Retention: 15 days of metrics and logs, 7 days of traces. Tempo also generates span metrics and the service graph |
| `provisioning/` | Grafana data sources (linked: metric exemplars → traces, logs ↔ traces) and dashboard loading |
| `dashboards/claude-headless-api.json` | The dashboard; edits made in Grafana can be exported back here |

The dashboard opens with **usage**: requests, active users and API tokens, conversations, tokens and cost, with per-user breakdowns and the model, effort, source and session mix. These are exact counts from the request logs in Loki. Below that it covers request rate, error ratio, Claude run latency, concurrency and queueing, tokens, cost,
cache hit ratio, API errors by code, HTTP latency per route, database connections, failed-request logs and
Claude run traces. It uses data-source variables, so it also imports into an existing Grafana.

To try it without spending tokens, run the app in test mode with export on (`api-test-mode-otel` in
`.claude/launch.json`) and drive it with `k6 run -e PROFILE=load loadtest/stress.js`.

Stop with `docker compose down`. Add `-v` to also delete the stack's own stored metrics, logs and traces;
this doesn't touch the app's data.
