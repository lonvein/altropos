# syntax=docker/dockerfile:1.7
#
# Application image: web UI + API shell. Model weights are NOT copied here.
# Python and Debian versions are pinned; update the digest deliberately and
# record the change in docs/deployment.md.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data/runtime

# OCR and PDF rendering belong to Egor's parsers; keep them in the runtime image
# so a later parser import does not require rebuilding the deployment contract.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        poppler-utils \
        libgl1 \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt

COPY navigator ./navigator
COPY scripts ./scripts

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/runtime \
    && chown -R app:app /app /data

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "navigator.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
