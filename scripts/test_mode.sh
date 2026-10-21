#!/usr/bin/env bash
# Run the app in test mode: tests/fake_claude.py answers instead of Claude, so nothing is spent.
# State lives in ./.test-mode/ (use --fresh to wipe it). The real .env is never used for credentials.
#
#   scripts/test_mode.sh [--fresh]
#   PORT=8020  FAKE_LATENCY_MS=800-2500  FAKE_ERROR_RATE=0  MAX_CONCURRENCY=4   (all optional)
set -euo pipefail
cd "$(dirname "$0")/.."

DIR=.test-mode
[[ ${1:-} == --fresh ]] && rm -rf "$DIR"
mkdir -p "$DIR"

unset ANTHROPIC_API_KEY
export SECRET_KEY=test-mode ADMIN_USERNAME=admin ADMIN_PASSWORD=test-mode-password \
  SESSION_COOKIE_SECURE=false CLAUDE_CODE_OAUTH_TOKEN= \
  DATABASE_URL="sqlite:///./$DIR/app.db" CLAUDE_DATA_DIR="./$DIR/claude" \
  CLAUDE_BIN="$PWD/tests/fake_claude.py" \
  CLAUDE_ENV_PASSTHROUGH=FAKE_CLAUDE_LATENCY_MS,FAKE_CLAUDE_ERROR_RATE \
  FAKE_CLAUDE_LATENCY_MS="${FAKE_LATENCY_MS:-800-2500}" \
  FAKE_CLAUDE_ERROR_RATE="${FAKE_ERROR_RATE:-0}"

if [[ -n ${MAX_CONCURRENCY:-} ]]; then
  .venv/bin/python -c "
from app.main import create_schema
from app.db import session_factory
from app import runtime_settings
create_schema()
runtime_settings.set_many(session_factory(), {'max_concurrency': int('$MAX_CONCURRENCY')})
print('max_concurrency =', $MAX_CONCURRENCY)"
fi

echo "Test mode on http://localhost:${PORT:-8020}  (admin / test-mode-password, fake latency ${FAKE_CLAUDE_LATENCY_MS} ms)"
exec .venv/bin/uvicorn app.main:app --port "${PORT:-8020}" --no-access-log
