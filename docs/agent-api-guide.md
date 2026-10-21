# Claude Headless API

> An OpenAI-compatible chat API backed by Claude (via Claude Code's headless mode), with server-side
> conversations ("sessions"). If your tool already speaks the OpenAI Chat Completions API, set the base URL
> to `http://localhost:8000/v1` and send a `chk_…` API token as the key.

This guide is for AI agents and programs that **call** this API. It is also served at `/llms.txt`. The full
machine-readable contract is `/openapi.json` (or `/openapi.yaml`), and the human reference is `/docs`.

## Essentials

- Base URL: `http://localhost:8000/v1`
- Auth header: `Authorization: Bearer chk_…` (or `x-api-key: chk_…`). Tokens come from the server's administrator.
- Content type: `application/json` in and out. Text only.
- One request returns one complete reply, or a stream of it with `stream: true` (see Streaming).

## Make a call

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "effort": "low", "messages": [{"role": "user", "content": "Hello"}]}'
```

The reply text is at `choices[0].message.content`.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="chk_...")
r = client.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "Hello"}],
                                   extra_body={"effort": "low"})
print(r.choices[0].message.content)
```

## Streaming

Add `"stream": true` to get the reply as server-sent events, exactly like OpenAI's API:
`data: {chat.completion.chunk}` lines with pieces in `choices[0].delta.content`, then `data: [DONE]`. OpenAI SDKs
handle this with `stream=True`. It works on `/v1/chat/completions` (with or without `session_id`) and on
`/v1/sessions/<id>/messages`.

- `"stream_options": {"include_usage": true}` adds a final chunk with `usage` before `[DONE]`.
- Errors before any text arrive as a normal JSON error response (check the HTTP status first).
- An error after text has started arrives as one `data: {"error": {…}}` event and the stream ends without
  `[DONE]`. Treat the partial text as incomplete.
- If you disconnect, the run stops. An interrupted session turn is not stored, so send it again.

## Choosing `model` and `effort`

Call `GET /v1/models` once to see what your token may use. Typical ids are `haiku`, `sonnet`, `opus` and
`fable` (aliases for the latest model of each family) or full ids such as `claude-sonnet-5-5`.

| Need | model | effort |
|---|---|---|
| Classification, extraction, short rewrites, routing | `haiku` | `low` |
| General Q&A, summaries, drafting | `sonnet` | `low` or `medium` |
| Code, multi-step reasoning, analysis | `sonnet` or `opus` | `high` |
| The hardest problems, where time and cost don't matter | `opus` | `xhigh` or `max` |

- Effort values: `low` < `medium` < `high` < `xhigh` < `max`. Higher means slower and more expensive.
- `effort` wins over OpenAI's `reasoning_effort`; `none`/`minimal` are treated as `low`.
- Omitting either uses the server's defaults. Your token may cap the effort: you get `403 effort_not_allowed`
  if you exceed it.

## Stateless calls vs sessions

**Stateless** (`POST /v1/chat/completions` without `session_id`): send the whole conversation every time.
Use it for one-off tasks or when you manage history yourself.

**Sessions**: the server keeps the conversation, and you send only the new message each turn. Use them for
long or multi-step conversations: less to send each turn, and Claude can usually reuse its cache.

```
POST /v1/sessions                      {"model": "sonnet", "system_prompt": "…"}   -> {"id": "<uuid>", …}
POST /v1/sessions/<id>/messages        {"content": "first message"}               -> chat.completion + "session_id"
POST /v1/sessions/<id>/messages        {"content": "follow-up", "effort": "high"} -> …
GET  /v1/sessions/<id>                 -> session with all messages
DELETE /v1/sessions/<id>               -> {"deleted": true}
```

- Equivalent through the OpenAI SDK: `extra_body={"session_id": "new"}` on the first call. Read
  `response.model_extra["session_id"]` and pass it on later calls; send **only the newest user message** in
  `messages`.
- The system prompt is fixed when the session is created. Model and effort may change on any turn.
- Turns in one session run one at a time. Don't send parallel turns to the same session; use separate
  sessions for parallel work.
- Sessions are shared by all tokens of the same user and invisible to other users.
- Delete sessions you no longer need. The administrator may also expire idle ones.

## Messages

- Roles: `system` / `developer` become the system prompt; `user` and `assistant` are the conversation.
  The last non-system message must be `user`.
- `content` is a string or a list of `{"type": "text", "text": "…"}` parts. Images, audio, files and
  `tool` / `function` messages are rejected.
- Structured output: `response_format: {"type": "json_schema", "json_schema": {"schema": {…}}}` returns the
  JSON document as a string in `content`. Parse it yourself.
- Ignored without error: `temperature`, `top_p`, `max_tokens`, `stop`, `seed`, `n`, `logprobs`, `tools`.
  To control length or format, ask for it in the prompt.

## Errors and retries

Errors look like `{"error": {"message": "…", "type": "…", "param": null, "code": "…"}}`.

| Status | `code` | What to do |
|---|---|---|
| 400 | `model_not_found`, `null` | Fix the request. Don't retry unchanged |
| 401 | `missing_api_key`, `invalid_api_key`, `api_key_revoked`, `api_key_expired` | Stop. Ask a human for a valid token |
| 403 | `model_not_allowed`, `effort_not_allowed`, `user_disabled` | Use an allowed model or a lower effort; otherwise stop |
| 404 | `session_not_found` | The session was deleted or expired. Start a new one and re-send the context you need |
| 429 | `rate_limit_exceeded` | Wait the number of seconds in the message, then retry |
| 502 | `upstream_error` | Retry up to 2 times with back-off (5 s, 20 s). If the message says "Not logged in", stop and tell a human |
| 504 | `timeout` | Retry once with a lower effort or a shorter task |

A failed session turn stores nothing, so retrying it is safe.

## Good citizenship

- Reuse one session per conversation instead of re-sending long histories.
- Prefer the lowest effort that does the job.
- Keep concurrency modest: the server runs a limited number of Claude processes and queues the rest.
- `usage.prompt_tokens` includes Claude's own context, so it is larger than your text. Don't use it to
  measure your prompt.
