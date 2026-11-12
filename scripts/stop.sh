#!/usr/bin/env bash
# Stop the stack. Named volumes are kept, so runtime data and weights survive.
#   ./scripts/stop.sh
#   ./scripts/stop.sh --purge --yes   # also delete the runtime volume
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PURGE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes) ASSUME_YES=1 ;;
    *) echo "Usage: $0 [--purge [--yes]]" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$ROOT/.env" ]]; then
  echo "No .env found; nothing to stop by this project file." >&2
  exit 1
fi

RUNTIME_VOLUME="$(grep -hE '^RUNTIME_VOLUME=' "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true)"
RUNTIME_VOLUME="${RUNTIME_VOLUME:-altropos_runtime}"

COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml")

"${COMPOSE[@]}" --profile models down --remove-orphans
echo "Stack stopped. Runtime volume '$RUNTIME_VOLUME' and models volume were kept."

if [[ "$PURGE" -eq 1 ]]; then
  if [[ "$ASSUME_YES" -ne 1 ]]; then
    echo "This deletes all documents, SQLite data and indexes in '$RUNTIME_VOLUME'."
    read -r -p "Type PURGE to continue: " answer
    if [[ "$answer" != "PURGE" ]]; then
      echo "Aborted; data was not deleted."
      exit 0
    fi
  fi
  docker volume rm "$RUNTIME_VOLUME" >/dev/null 2>&1 || true
  echo "Runtime volume '$RUNTIME_VOLUME' deleted."
  echo "The models volume is intentionally preserved. Remove it explicitly with:"
  echo "  docker volume rm altropos_models"
fi
