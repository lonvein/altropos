# syntax=docker/dockerfile:1.7
#
# Embeddings/reranker image. Weights live in the models volume and are mounted
# read-only; they are never baked into this image. Build with:
#   docker build -f Dockerfile.ml -t altropos-ml:local .
# For A100 pass --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu126
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-app.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt \
    && pip install --no-cache-dir --index-url "${TORCH_INDEX}" torch==2.7.1 \
    && pip install --no-cache-dir -r requirements-ml.txt

COPY navigator ./navigator
COPY services ./services

RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin ml \
    && chown -R ml:ml /app

USER ml

EXPOSE 8081

# The ML HTTP service has no model checks in GET; a TCP probe verifies that the
# process is listening without loading or sending source text.
HEALTHCHECK --interval=20s --timeout=3s --start-period=180s --retries=5 \
    CMD ["python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',8081),2); s.close()"]

CMD ["python", "-m", "services.ml.server", "--host", "0.0.0.0", "--port", "8081"]
