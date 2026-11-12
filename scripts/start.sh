#!/usr/bin/env bash
# Start Altropos with a hardware profile from config/profiles/*.env.
#
#   ./scripts/start.sh 4b          # CPU reserve profile
#   ./scripts/start.sh 3060ti      # RTX 3060 Ti
#   ./scripts/start.sh a100        # A100 + SSH tunnel for the browser
#   ./scripts/start.sh development # UI/API shell only, models disabled
#
# Without downloaded weights the application still starts, but model services
# are not launched and /health reports a limited mode. Use --with-models to
# make missing weights a hard error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-development}"
WITH_MODELS=0
if [[ "${2:-}" == "--with-models" || "${1:-}" == "--with-models" ]]; then
  WITH_MODELS=1
fi
if [[ "$PROFILE" == "--with-models" ]]; then
  PROFILE="development"
fi

PROFILE_FILE="$ROOT/config/profiles/$PROFILE.env"
if [[ ! -f "$PROFILE_FILE" ]]; then
  echo "Unknown profile '$PROFILE'. Available: development, a100, 3060ti, 4b" >&2
  exit 2
fi

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example (no secrets were added)."
fi

read_env() {
  local key="$1" default="${2:-}"
  local value
  value="$({ grep -hE "^${key}=" "$ROOT/.env" "$PROFILE_FILE" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-)"
  printf '%s' "${value:-$default}"
}

APP_PORT="$(read_env APP_PORT 8000)"
APP_BIND="$(read_env APP_BIND 127.0.0.1)"
RUNTIME_VOLUME="$(read_env RUNTIME_VOLUME altropos_runtime)"
MODELS_VOLUME="$(read_env MODELS_VOLUME altropos_models)"
LLM_GGUF_FILE="$(read_env LLM_GGUF_FILE Qwen3-4B-Q4_K_M.gguf)"
EMBEDDING_REVISION="$(read_env EMBEDDING_REVISION 5617a9f61b028005a4858fdac845db406aefb181)"
RERANKER_REVISION="$(read_env RERANKER_REVISION 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e)"

COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" --env-file "$PROFILE_FILE")
COMPOSE+=(-f "$ROOT/docker-compose.yml")
case "$PROFILE" in
  a100) COMPOSE+=(-f "$ROOT/compose.a100.yml") ;;
  3060ti) COMPOSE+=(-f "$ROOT/compose.3060ti.yml") ;;
esac

TMP_ENV=""
cleanup() {
  if [[ -n "$TMP_ENV" ]]; then rm -f "$TMP_ENV"; fi
}
trap cleanup EXIT

echo "Building application image for profile '$PROFILE'..."
"${COMPOSE[@]}" build app

MODELS_PRESENT=0
if docker run --rm --entrypoint /bin/sh \
      -v "$MODELS_VOLUME:/models:ro" \
      altropos-app:local \
      -c "test -f '/models/gguf/$LLM_GGUF_FILE' && test -f '/models/hf/hub/models--BAAI--bge-m3/snapshots/$EMBEDDING_REVISION/pytorch_model.bin' && test -f '/models/hf/hub/models--BAAI--bge-reranker-v2-m3/snapshots/$RERANKER_REVISION/model.safetensors'" \
      >/dev/null 2>&1; then
  MODELS_PRESENT=1
fi

if [[ "$MODELS_PRESENT" -eq 1 ]]; then
  COMPOSE+=(--profile models)
  echo "Weights found in volume '$MODELS_VOLUME'; starting LLM and embeddings-reranker."
else
  if [[ "$WITH_MODELS" -eq 1 ]]; then
    echo "Weights are missing. Run: ./scripts/download-models.sh $PROFILE" >&2
    exit 1
  fi
  TMP_ENV="$(mktemp)"
  printf 'LLM_ENABLED=false\nML_ENABLED=false\n' > "$TMP_ENV"
  COMPOSE+=(--env-file "$TMP_ENV")
  echo "WARNING: weights are missing. Starting app only; models stay disabled." >&2
  echo "         Download them with: ./scripts/download-models.sh $PROFILE" >&2
  echo "         Then rerun: ./scripts/start.sh $PROFILE --with-models" >&2
fi

"${COMPOSE[@]}" up -d --build

app_health() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "http://127.0.0.1:${APP_PORT}/health" 2>/dev/null
  else
    python3 - "$APP_PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
    sys.stdout.write(json.dumps(json.load(response), ensure_ascii=False))
PY
  fi
}

echo "Waiting for the application..."
APP_OK=0
for _ in $(seq 1 60); do
  if app_health >/dev/null; then
    APP_OK=1
    break
  fi
  sleep 2
done

if [[ "$MODELS_PRESENT" -eq 1 ]]; then
  echo "Waiting for the model services (first load can take several minutes)..."
  for _ in $(seq 1 150); do
    llm_id="$("${COMPOSE[@]}" ps -q llm 2>/dev/null || true)"
    ml_id="$("${COMPOSE[@]}" ps -q embeddings-reranker 2>/dev/null || true)"
    llm_state="missing"; ml_state="missing"
    if [[ -n "$llm_id" ]]; then
      llm_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$llm_id" 2>/dev/null || echo unknown)"
    fi
    if [[ -n "$ml_id" ]]; then
      ml_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$ml_id" 2>/dev/null || echo unknown)"
    fi
    if [[ "$llm_state" == "healthy" && "$ml_state" == "healthy" ]]; then
      break
    fi
    sleep 2
  done
fi

echo
echo "Profile:   $PROFILE"
echo "App:       http://${APP_BIND}:${APP_PORT}"
echo "Runtime:   volume $RUNTIME_VOLUME"
echo "Models:    volume $MODELS_VOLUME ($([[ "$MODELS_PRESENT" -eq 1 ]] && echo enabled || echo disabled))"
if app_health >/dev/null; then
  echo "Health:    OK"
else
  echo "Health:    NOT READY (see: docker compose logs app)" >&2
fi
if [[ "$APP_BIND" != "127.0.0.1" && "$APP_BIND" != "localhost" ]]; then
  echo "WARNING: APP_BIND=$APP_BIND publishes beyond localhost. Use an SSH tunnel instead." >&2
fi
if [[ "$APP_BIND" == "127.0.0.1" ]]; then
  echo "Remote:    ssh -N -L ${APP_PORT}:127.0.0.1:${APP_PORT} <user>@<host>"
fi
echo "Smoke:     ./scripts/smoke.sh"
echo "Stop:      ./scripts/stop.sh"
echo "Backup:    ./scripts/backup.sh"

[[ "$APP_OK" -eq 1 ]]
