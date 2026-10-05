#!/usr/bin/env bash
# Local dev server: mock model, its own DB, the carrier portal served by this same app.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
export TICKETPILOT_PROVIDER="${TICKETPILOT_PROVIDER:-mock}"
export TICKETPILOT_DB="${TICKETPILOT_DB:-var/demo.db}"
export TICKETPILOT_PORTAL_BASE_URL="${TICKETPILOT_PORTAL_BASE_URL:-http://127.0.0.1:$PORT}"
exec .venv/bin/python -m uvicorn ticketpilot.web.app:create_app --factory --host 127.0.0.1 --port "$PORT"
