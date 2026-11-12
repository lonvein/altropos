#!/usr/bin/env bash
# Restore a snapshot produced by scripts/backup.sh. The runtime volume is
# replaced as a whole; the models volume is not touched.
#
#   ./scripts/restore.sh backups/altropos-runtime-...tar.gz --yes
#   ./scripts/restore.sh ... --no-pre-backup --yes
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE=""
ASSUME_YES=0
PRE_BACKUP=1
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    --no-pre-backup) PRE_BACKUP=0 ;;
    --*) echo "Usage: $0 ARCHIVE [--yes] [--no-pre-backup]" >&2; exit 2 ;;
    *) ARCHIVE="$arg" ;;
  esac
done

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Snapshot archive not found: ${ARCHIVE:-<empty>}" >&2
  exit 2
fi
if [[ ! -f "$ROOT/.env" ]]; then
  echo "Copy .env.example to .env before restoring." >&2
  exit 1
fi
if ! docker image inspect altropos-app:local >/dev/null 2>&1; then
  echo "Image altropos-app:local is missing. Build it with ./scripts/start.sh development." >&2
  exit 1
fi

ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
if [[ -f "$ARCHIVE.sha256" ]]; then
  echo "Verifying archive checksum..."
  ( cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256" )
else
  echo "WARNING: no .sha256 file; the archive integrity is not verified." >&2
fi

read_env() {
  grep -hE "^$1=" "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true
}
RUNTIME_VOLUME="${RUNTIME_VOLUME:-$(read_env RUNTIME_VOLUME)}"
RUNTIME_VOLUME="${RUNTIME_VOLUME:-altropos_runtime}"
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml")

if [[ "$ASSUME_YES" -ne 1 ]]; then
  echo "This replaces every file in runtime volume '$RUNTIME_VOLUME'."
  read -r -p "Type RESTORE to continue: " answer
  if [[ "$answer" != "RESTORE" ]]; then
    echo "Aborted; data was not changed."
    exit 0
  fi
fi

echo "Stopping the application..."
"${COMPOSE[@]}" stop app >/dev/null 2>&1 || true

if [[ "$PRE_BACKUP" -eq 1 ]]; then
  echo "Saving the current state before restore..."
  "$ROOT/scripts/backup.sh" --label pre-restore
fi

echo "Clearing runtime volume '$RUNTIME_VOLUME'..."
docker run --rm --user root -v "$RUNTIME_VOLUME:/data" --entrypoint /bin/sh \
  altropos-app:local -c 'find /data -mindepth 1 -delete'

echo "Extracting snapshot..."
docker run --rm --user root \
  -v "$RUNTIME_VOLUME:/data" \
  -v "$(dirname "$ARCHIVE"):/backup:ro" \
  --entrypoint tar \
  altropos-app:local \
  -xzf "/backup/$(basename "$ARCHIVE")" -C /data

# Extraction runs as root; restore ownership to the app user (uid/gid 10001)
# so SQLite, originals and indexes remain writable after the restart.
docker run --rm --user root \
  -v "$RUNTIME_VOLUME:/data" \
  --entrypoint /bin/sh \
  altropos-app:local \
  -c 'chown -R 10001:10001 /data'

echo "Starting the application..."
"${COMPOSE[@]}" up -d app >/dev/null

echo "Checking SQLite integrity..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T app python -c '
import os, sqlite3
from pathlib import Path
path = Path(os.environ.get("DATA_DIR", "/data/runtime")) / "navigator.sqlite"
if not path.exists():
    raise SystemExit("no database in restored snapshot")
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
result = con.execute("PRAGMA integrity_check").fetchone()[0]
con.close()
if result != "ok":
    raise SystemExit(f"integrity_check: {result}")
print("SQLite integrity: ok")
' 2>/dev/null; then
    echo "Restore completed: $ARCHIVE"
    echo "The embedding fingerprint is checked by Klim's index loader, not by this script."
    exit 0
  fi
  sleep 2
done

echo "Restore extracted, but the integrity check did not pass. Inspect: docker compose logs app" >&2
exit 1
