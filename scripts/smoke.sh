#!/usr/bin/env bash
# Run one real request against the running stack. Reads settings from the app
# container, so no model ports need to be published on the host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$ROOT/.env" ]]; then
  echo "No .env; start the stack first with ./scripts/start.sh development" >&2
  exit 1
fi
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml")
if [[ -z "$("${COMPOSE[@]}" ps -q app 2>/dev/null || true)" ]]; then
  echo "App container is not running. Start it with ./scripts/start.sh <profile>" >&2
  exit 1
fi

"${COMPOSE[@]}" exec -T app python scripts/smoke.py
