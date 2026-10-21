# Development recipes

Step-by-step guides for common changes. The rules in [AGENTS.md](../AGENTS.md) apply throughout, above all:
leave `.env` and the Docker volume alone, and never call the real Claude CLI from tests.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
```

Run the app locally with the `api-dev` configuration in `.claude/launch.json` (port 8010, `./dev.db`,
`./.dev-claude`), or the equivalent `uvicorn` command in AGENTS.md. Sign in with `admin` / `dev-password`.
Add `CLAUDE_BIN=tests/fake_claude.py` to the environment to get canned replies instead of
`502 Not logged in`. Delete `dev.db*` and `.dev-claude/` when you are done.

## Adding or changing a `/v1` endpoint

1. **Route** in `app/routers/api.py`:
   - If it runs Claude, wrap the body in `_logged(request, db, "<endpoint.name>", handle)`. That gives you
     auth, error mapping and the request log row for free.
   - If it doesn't run Claude, call `_authed(db, request)` and catch `APIError`, as the session CRUD routes do.
   - Parse bodies with `_parse(request, SomeModel)`. Pydantic models use `ConfigDict(extra="ignore"` or
     `"allow")` so OpenAI-style clients can send extra fields.
   - Run access checks (`_check_access`, `_check_rate_limit`) before spending anything.
2. **Errors:** raise `APIError(status, message, type, code)`. If you introduce a new `code`, add it to the
   `Error` schema description in the spec.
3. **Spec:** add the path to `docs/openapi.yaml` with:
   - `operationId`, `summary`, `description`, request and response schemas (reuse `components`);
   - at least one example per request and success response;
   - `x-codeSamples` for cURL, Python and JavaScript (`lang: bash | python | javascript`; the tests require
     all three for `/v1` paths);
   - the `$ref` error responses that can occur.

   Use `http://localhost:8000` as the base URL in examples: the docs page swaps in the real one. Then run
   `.venv/bin/python scripts/export_openapi.py`.
4. **Tests:**
   - Behaviour goes in `tests/test_api.py` or `tests/test_sessions.py`.
   - Add the live response to `test_live_responses_match_spec` in `tests/test_docs.py` so its schema is
     checked against reality.
   - The route-coverage test fails until steps 1 and 3 agree.
5. **Docs:** README's *Use* section if it is user-visible; docs/agent-api-guide.md if agents should know
   about it.

## Adding a runtime (admin-editable) setting

1. Add the key and default to `DEFAULTS` in `app/runtime_settings.py`.
2. Read it with `runtime_settings.get(db, key)` or from the `settings` dict passed through `_logged`.
3. Expose it in `app/templates/settings.html` and parse and validate it in `settings_save()`
   (`app/routers/admin.py`). Invalid input should `flash(..., "error")` and redirect back without saving.
4. Extend `test_settings_validation_and_save` in `tests/test_admin.py`.
5. Mention it in docs/architecture.md (configuration table) if it changes behaviour.

## Adding a process (env) setting

1. Add a field to `Settings` in `app/config.py`. The env var name is the upper-case field name.
2. Add it, commented, to `.env.example` (never to the operator's `.env`) and to the README's variable table.
3. If the **Claude CLI** needs it, add it to `_ENV_PASSTHROUGH` in `app/claude_runner.py` deliberately.
   The child env is an allowlist.

## Changing the database schema

- **New nullable column:** add it to the model. `create_schema()` adds it to existing databases at startup.
  Make sure code handles `None` for old rows.
- **New table:** add the model. `create_all` creates it.
- **Anything else** (rename, type change, `NOT NULL` without a server default, dropping):
  1. Write the SQL by hand. SQLite usually needs "create new table, copy, drop, rename".
  2. Document it in the PR and in docs/operations.md under *Upgrading*.
  3. Test it against a copy of a real database, never the operator's live volume.
- `test_schema_upgrade_adds_new_columns` in `tests/test_sessions.py` shows how to test an upgrade from an
  older schema.

## Adding a dashboard page

1. Add the route in `app/routers/admin.py`:
   - GET handlers take `admin: User = Depends(require_admin)` and return
     `render(request, "page.html", admin, nav="<key>", ...)`.
   - Mutations use `@post("/path")`, which enforces CSRF, and end with `flash(...)` + `back(url)`.
2. Add the template, extending `base.html`:
   - Import macros with `{% from "_macros.html" import csrf_field, badge with context %}`.
   - Every form needs `{{ csrf_field() }}`.
   - Use the existing classes (`card`, `grid`/`stat`, `table-wrap`, `form-row`, `field`, `actions`, `badge b-*`).
3. Add the nav entry to the list in `base.html`, if it is a top-level page.
4. Style only with the CSS variables in `app/static/app.css`. Check light, dark and narrow (< 860 px) widths.
5. Add a test in `tests/test_admin.py` that renders the page and exercises its POSTs, including a CSRF failure.

## Changing how Claude is invoked

- All argv construction is in `build_argv()` (`app/claude_runner.py`). Check flags against `claude --help`
  for the pinned `CLAUDE_CODE_VERSION`.
- Teach `tests/fake_claude.py` the new flag if behaviour depends on it, and assert on argv via
  `FAKE_CLAUDE_ARGS_FILE` (see `test_argv_and_isolation`).
- Update the flag table in docs/architecture.md.
- Verifying against the real CLI needs a logged-in Claude and costs quota, so ask the operator first.
  Never put a real token in a test.

## Front-end changes

- **No build step, frameworks or CDNs.** Plain `app/static/*.js` (IIFEs, event delegation) and `app.css`.
- **Theme:** new colours become variables in `:root` and in both dark blocks (the media-query one and
  `[data-theme="dark"]`).
- **Escaping:** user and model content is escaped by Jinja. `chat.js` renders Markdown with an
  escape-first renderer, so never assign untrusted text to `innerHTML` without `esc()`.
- **Caching:** browsers cache static files, so hard-reload when checking CSS/JS changes.

## Releasing a change to the container

The operator does this (see docs/operations.md): `docker compose up -d --build`. It rebuilds the image and
recreates the container while keeping `.env` and the data volume. Agents should not run it unless asked.
