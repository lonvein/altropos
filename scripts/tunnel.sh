#!/usr/bin/env bash
# Open an SSH tunnel to an Altropos host where the app binds 127.0.0.1:8000.
#   ./scripts/tunnel.sh user@a100-host
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${SSH_TARGET:-}}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 <user>@<host>   (or export SSH_TARGET=...)" >&2
  exit 2
fi
APP_PORT="$(grep -hE '^APP_PORT=' "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
APP_PORT="${APP_PORT:-8000}"
echo "Opening localhost:$APP_PORT -> $TARGET:127.0.0.1:$APP_PORT"
exec ssh -N -o ExitOnForwardFailure=yes -L "${APP_PORT}:127.0.0.1:${APP_PORT}" "$TARGET"
