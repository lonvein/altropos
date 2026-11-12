#!/usr/bin/env bash
# Download pinned weights into the persistent Docker volume.
#
#   ./scripts/download-models.sh 4b        # reserve profile + BGE
#   ./scripts/download-models.sh a100      # 14B shards + BGE
#   ./scripts/download-models.sh ml        # only embeddings/reranker
#   ./scripts/download-models.sh all       # every LLM profile + BGE (~20 GiB)
#
# No token is stored. If Hugging Face requires one, export HF_TOKEN in the shell.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${PROFILE:-4b}}"
VOLUME="${MODELS_VOLUME:-altropos_models}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df}"

case "$TARGET" in
  a100|3060ti|4b|ml|all) ;;
  *)
    echo "Usage: $0 [a100|3060ti|4b|ml|all]" >&2
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to download weights into the models volume." >&2
  exit 1
fi

docker volume create "$VOLUME" >/dev/null

EXTRA_ENV=()
if [[ -n "${HF_TOKEN:-}" ]]; then
  # Pass by name so the token does not appear in the process arguments.
  EXTRA_ENV+=(-e HF_TOKEN)
fi

echo "Downloading target '$TARGET' into volume '$VOLUME' (lock: config/models.lock.json)"
docker run --rm "${EXTRA_ENV[@]}" \
  -v "$VOLUME:/models" \
  -v "$ROOT:/workspace:ro" \
  -w /workspace \
  "$PYTHON_IMAGE" \
  sh -c "pip install --quiet --no-cache-dir 'huggingface_hub==2.4.0' && exec python scripts/download_models.py --root /models --target '$TARGET'"
