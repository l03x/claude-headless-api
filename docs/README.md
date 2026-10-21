# Documentation

| Document | For | What's in it |
|---|---|---|
| [../AGENTS.md](../AGENTS.md) | Anyone changing the code (agents first) | Rules, commands, repo map, conventions, testing, definition of done |
| [architecture.md](architecture.md) | Understanding the system | Request lifecycle, how `claude -p` is invoked, sessions, data model, configuration, security, docs pipeline |
| [development.md](development.md) | Making a change | Recipes: endpoints, settings, schema changes, dashboard pages, CLI flags, front end |
| [operations.md](operations.md) | Running a deployment | Deploy, rotate the Claude token, upgrade, back up and restore, reset a password, tuning, troubleshooting |
| [agent-api-guide.md](agent-api-guide.md) | Agents and programs *calling* the API | Auth, model and effort choice, sessions, retry policy. Served at `/llms.txt` |
| [openapi.yaml](openapi.yaml) / [openapi.json](openapi.json) | Client generators, tools | The API contract (YAML is the source; JSON is generated). Rendered at `/docs` |
| [missing-features.md](missing-features.md) | Planning | What isn't supported yet and what adding it would take |
