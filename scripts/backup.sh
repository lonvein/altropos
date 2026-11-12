#!/usr/bin/env bash
# Create one consistent snapshot of the runtime volume: SQLite, originals,
# extracted text and indexes. The app is stopped first; a live SQLite copy is
# never archived.
#
#   ./scripts/backup.sh
#   ./scripts/backup.sh --label before-update
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="snapshot"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="${2:?missing label}"; shift 2 ;;
    *) echo "Usage: $0 [--label NAME]" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Copy .env.example to .env before backing up." >&2
  exit 1
fi
if ! docker image inspect altropos-app:local >/dev/null 2>&1; then
  echo "Image altropos-app:local is missing. Run ./scripts/start.sh development first." >&2
  exit 1
fi

read_env() {
  grep -hE "^$1=" "$ROOT/.env" | tail -n 1 | cut -d= -f2- || true
}
RUNTIME_VOLUME="${RUNTIME_VOLUME:-$(read_env RUNTIME_VOLUME)}"
RUNTIME_VOLUME="${RUNTIME_VOLUME:-altropos_runtime}"
PROFILE="$(read_env PROFILE)"; PROFILE="${PROFILE:-development}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="altropos-runtime-${STAMP}-${LABEL}.tar.gz"

COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml")
WAS_RUNNING=0
if [[ -n "$("${COMPOSE[@]}" ps -q app 2>/dev/null || true)" ]]; then
  WAS_RUNNING=1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "Stopping the application to quiesce SQLite..."
"${COMPOSE[@]}" stop app >/dev/null 2>&1 || true

echo "Checkpointing SQLite WAL..."
"${COMPOSE[@]}" run --rm --no-deps app python -c '
import os, sqlite3
from pathlib import Path
path = Path(os.environ.get("DATA_DIR", "/data/runtime")) / "navigator.sqlite"
if path.exists():
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print("checkpoint ok")
else:
    print("no database yet; archiving documents/indexes only")
' >/dev/null

echo "Archiving volume '$RUNTIME_VOLUME' -> $ARCHIVE"
docker run --rm --user root \
  -v "$RUNTIME_VOLUME:/data:ro" \
  -v "$BACKUP_DIR:/backup" \
  --entrypoint tar \
  altropos-app:local \
  -czf "/backup/$ARCHIVE" -C /data .

# tar runs as root to read the app-owned volume, so the archive is root-owned
# on the host. Return it to the invoking user before checksums and chmod.
docker run --rm --user root \
  -v "$BACKUP_DIR:/backup" \
  --entrypoint /bin/sh \
  altropos-app:local \
  -c "chown $(id -u):$(id -g) '/backup/$ARCHIVE'"

if ! tar -tzf "$BACKUP_DIR/$ARCHIVE" >/dev/null 2>&1; then
  echo "Archive verification failed: $BACKUP_DIR/$ARCHIVE" >&2
  exit 1
fi

( cd "$BACKUP_DIR" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256" )
chmod 600 "$BACKUP_DIR/$ARCHIVE" "$BACKUP_DIR/$ARCHIVE.sha256"

GIT_REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$BACKUP_DIR/$ARCHIVE.json" <<EOF
{
  "archive": "$ARCHIVE",
  "created_utc": "$STAMP",
  "profile": "$PROFILE",
  "git_revision": "$GIT_REV",
  "runtime_volume": "$RUNTIME_VOLUME",
  "contents": "SQLite (WAL checkpointed), originals, extracted files, indexes",
  "restore": "./scripts/restore.sh $BACKUP_DIR/$ARCHIVE --yes"
}
EOF
chmod 600 "$BACKUP_DIR/$ARCHIVE.json"

echo
echo "Snapshot created: $BACKUP_DIR/$ARCHIVE"
echo "Checksum:         $BACKUP_DIR/$ARCHIVE.sha256"
echo "Metadata:         $BACKUP_DIR/$ARCHIVE.json"

if [[ "$WAS_RUNNING" -eq 1 ]]; then
  echo "Restarting the application..."
  "${COMPOSE[@]}" up -d app >/dev/null
fi
