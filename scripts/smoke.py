#!/usr/bin/env python3
"""One real request to each configured local service, plus the app health page.

Run inside the app container so service names resolve:
    docker compose exec -T app python scripts/smoke.py
No retries and no fallback: a failed component makes the command fail.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request

from navigator.config import Settings
from navigator.schemas import EmbeddingResponse, Fragment, RerankResponse

APP_HEALTH = "http://127.0.0.1:8000/health"
QUESTION = "Ответь одним словом: сколько дней в неделе?"
FRAGMENT = Fragment(
    id="smoke-fragment-1",
    document_id="smoke-document-1",
    document_version="smoke-version-1",
    ordinal=0,
    text="ИСКУССТВЕННЫЙ ПРИМЕР: проверка локального reranker. Не является документом.",
    section="0",
    locator={"kind": "pdf", "page": 1},
    )


def post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def get_json(url: str, timeout: float = 3) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read())


def check_app(settings: Settings) -> None:
    started = time.monotonic()
    health = get_json(APP_HEALTH)
    elapsed = round((time.monotonic() - started) * 1000)
    print(f"app /health: profile={health.get('profile')} status={health.get('status')} ({elapsed} ms)")
    if health.get("profile") != settings.profile:
        raise SystemExit(f"health profile mismatch: {health.get('profile')} != {settings.profile}")
    if health.get("status") not in {"ok", "limited", "degraded"}:
        raise SystemExit(f"unexpected health status: {health.get('status')}")


def check_ml(settings: Settings) -> None:
    started = time.monotonic()
    payload = {"texts": ["ИСКУССТВЕННЫЙ ПРИМЕР: проверка embeddings."], "purpose": "query"}
    data = post_json(settings.ml_url.rstrip("/") + "/embed", payload, settings.request_timeout)
    parsed = EmbeddingResponse.model_validate(data)
    if parsed.fingerprint != settings.fingerprint:
        raise SystemExit(f"embedding fingerprint mismatch: {parsed.fingerprint}")
    norm = math.sqrt(sum(value * value for value in parsed.vectors[0]))
    print(f"ml /embed: dim={len(parsed.vectors[0])} norm={norm:.4f} "
          f"({round((time.monotonic() - started) * 1000)} ms)")

    started = time.monotonic()
    rerank = post_json(
        settings.ml_url.rstrip("/") + "/rerank",
        {"question": "Проверка reranker", "fragments": [FRAGMENT.model_dump(mode="json")]},
        settings.request_timeout,
    )
    scores = RerankResponse.model_validate(rerank)
    if not scores.scores or scores.scores[0].fragment_id != FRAGMENT.id:
        raise SystemExit("reranker did not return the submitted fragment")
    print(f"ml /rerank: score={scores.scores[0].score:.4f} "
          f"({round((time.monotonic() - started) * 1000)} ms)")


def check_llm(settings: Settings) -> None:
    started = time.monotonic()
    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": QUESTION}],
        "temperature": 0,
        "max_tokens": 48,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = post_json(settings.llm_url.rstrip("/") + "/chat/completions", payload, settings.request_timeout)
    choice = (data.get("choices") or [None])[0]
    if not choice:
        raise SystemExit(f"LLM response has no choices: {data}")
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise SystemExit(f"LLM returned empty content: {message}")
    if message.get("reasoning_content"):
        raise SystemExit("LLM returned reasoning_content although thinking is disabled")
    print(f"llm /chat/completions: {content[:120]!r} "
          f"({round((time.monotonic() - started) * 1000)} ms)")
    print(f"llm finish_reason={choice.get('finish_reason')}")


def main() -> int:
    settings = Settings()
    print(f"profile={settings.profile} ml_enabled={settings.ml_enabled} "
          f"llm_enabled={settings.llm_enabled} thinking={settings.thinking_enabled}")
    check_app(settings)
    if settings.ml_enabled:
        check_ml(settings)
    else:
        print("ml: disabled by configuration (skip)")
    if settings.llm_enabled:
        check_llm(settings)
    else:
        print("llm: disabled by configuration (skip)")
    print("smoke: OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        sys.exit(1)
