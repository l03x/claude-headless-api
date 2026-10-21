@AGENTS.md

## Claude Code specifics

- Preview UI changes with the `api-dev` configuration in `.claude/launch.json` (port 8010). It never reads or
  changes the operator's stack on port 8000.
- The built-in browser keeps its own cookies. Logging in to the dev server doesn't affect the operator's browser.
