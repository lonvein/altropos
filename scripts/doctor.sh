#!/usr/bin/env bash
# Preflight checks before a clean deployment. Never downloads weights.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-development}"
FAIL=0
warn() { echo "WARN: $*" >&2; }
fail() { echo "FAIL: $*" >&2; FAIL=1; }

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable (permissions or service)"
  docker compose version >/dev/null 2>&1 || fail "docker compose plugin is not available"
fi

if [[ ! -f "$ROOT/.env" ]]; then
  warn ".env is missing; scripts/start.sh will copy .env.example"
fi

PROFILE_FILE="$ROOT/config/profiles/$PROFILE.env"
[[ -f "$PROFILE_FILE" ]] || fail "unknown profile '$PROFILE'"

if [[ "$PROFILE" != "development" && "$PROFILE" != "4b" ]]; then
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    warn "nvidia-smi not found: GPU profiles A100/3060 Ti cannot be validated on this host"
  else
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
  fi
  if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    warn "Docker runtime 'nvidia' not detected; install nvidia-container-toolkit before GPU start"
  fi
fi

FREE_GIB="$(df -Pk "$ROOT" | awk 'NR==2 {printf "%d", $4/1024/1024}')"
if [[ "${FREE_GIB:-0}" -lt 10 ]]; then
  warn "less than 10 GiB free on the repository filesystem (weights need ~7-20 GiB)"
else
  echo "Free disk: ${FREE_GIB} GiB"
fi

echo "RAM:"
free -h || true
echo "CPUs: $(nproc 2>/dev/null || echo unknown)"

if command -v ss >/dev/null 2>&1; then
  APP_PORT="$(grep -hE '^APP_PORT=' "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  APP_PORT="${APP_PORT:-8000}"
  if ss -ltn "sport = :$APP_PORT" 2>/dev/null | tail -n +2 | grep -q .; then
    warn "port $APP_PORT is already in use"
  fi
fi

if [[ "$FAIL" -eq 1 ]]; then
  echo "Preflight failed." >&2
  exit 1
fi
echo "Preflight OK for profile '$PROFILE' (hardware-specific items above may remain unverified)."
