# Claude Headless API

An OpenAI-compatible LLM API backed by Claude Code's headless mode (`claude -p`), with API-token auth,
server-side sessions and an admin dashboard. One Docker container.

> [!IMPORTANT]
> **Not affiliated with Anthropic.** This is an independent project. It is not made, endorsed or supported by
> Anthropic, PBC. "Claude" and "Claude Code" are trademarks of Anthropic.

## Terms of use

Every request runs the official Claude Code CLI, so Anthropic's terms apply to how you deploy it. What follows
is a summary of Anthropic's [Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance),
not legal advice:

| | |
|---|---|
| ✅ **Subscription token for yourself** | `CLAUDE_CODE_OAUTH_TOKEN` (Free/Pro/Max) is meant for the subscriber's own, ordinary, individual use, and plan limits assume that. Keep every API token for yourself. For heavy automation, use an API key instead. |
| ❌ **Subscription token for other people** | Don't issue API tokens to others on a subscription login, share it, or route anyone else's requests through it. Anthropic forbids routing requests through Free/Pro/Max credentials on behalf of users. |
| ✅ **API key for your team** | With an `ANTHROPIC_API_KEY` ([Commercial Terms](https://www.anthropic.com/legal/commercial-terms)) you can serve your own authorized users, as long as usage is billed to you. |
| ❌ **Reselling** | Never charge others for access or resell usage, whichever credential you use. |

All requests are also subject to the [Usage Policy](https://www.anthropic.com/legal/aup). Anthropic may enforce
these terms without notice. If you are unsure about your case, [ask Anthropic](https://www.anthropic.com/contact-sales).

## Features

- `POST /v1/chat/completions` and `GET /v1/models`: OpenAI SDKs work by changing `base_url`.
- Pick the model (`opus`, `sonnet`, `haiku`, `fable` or a full id) and the effort (`low` … `max`) per request.
- Sessions (`/v1/sessions`) resume the same Claude Code conversation turn after turn.
- Admin dashboard (`/admin`):
  - a chat with Claude;
  - users, and API tokens with expiry, model allow-list, rate limit and effort cap;
  - sessions, logs and settings;
  - a light/dark theme.
- API reference at `/docs`, a brief for AI agents at `/llms.txt`.
- Claude runs with no tools: a plain text model.

## Deploy

```bash
cp .env.example .env
```

| Variable | |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` **or** `ANTHROPIC_API_KEY` | Claude credential, set exactly one. Get a subscription token from `claude setup-token`; see [Terms of use](#terms-of-use) |
| `SECRET_KEY` | Random string that signs admin sessions |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | First admin, created when no admin exists |
| `PORT` | Host port (default `8000`) |
| `SESSION_COOKIE_SECURE` | `true` behind HTTPS |
| `CLAUDE_CODE_VERSION` | Claude Code version baked into the image |

```bash
docker compose up -d --build
```

Sign in at `http://localhost:8000/admin`, create a user and an API token, then:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...")
r = client.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "Hello"}],
                                   extra_body={"effort": "high"})
print(r.choices[0].message.content)
```

## Documentation

| | |
|---|---|
| `/docs` · [`docs/openapi.yaml`](docs/openapi.yaml) | API reference with examples |
| [`docs/operations.md`](docs/operations.md) | Upgrades, backups, token rotation, troubleshooting |
| [`docs/architecture.md`](docs/architecture.md) · [`docs/development.md`](docs/development.md) | How it works, how to change it |
| [`docs/missing-features.md`](docs/missing-features.md) | Known limitations |
| [`AGENTS.md`](AGENTS.md) | Guide for coding agents |

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
```

Load test with [k6](https://k6.io) against test mode (fake Claude CLI, no tokens spent):

```bash
scripts/test_mode.sh --fresh                      # terminal 1: app on :8020
k6 run -e PROFILE=stress loadtest/stress.js       # terminal 2: smoke | load | stress | spike | soak
```
