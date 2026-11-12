from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from navigator.config import Settings

log = logging.getLogger("navigator.ui.health")


async def _probe(url: str, timeout: float = 1.5) -> dict[str, Any]:
    """Best-effort reachability probe; a missing service must not break the UI."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.TimeoutException:
        return {"status": "unavailable", "reason": "Сервис не ответил за отведённое время."}
    except Exception as exc:  # noqa: BLE001 - health must never raise to the page
        log.info("Health probe failed for %s: %s", url, type(exc).__name__)
        return {"status": "unavailable", "reason": "Соединение с сервисом не установлено."}

    code = response.status_code
    if 200 <= code < 300:
        return {"status": "ok", "http_status": code}
    if code < 500:
        return {
            "status": "degraded",
            "http_status": code,
            "reason": "Сервис отвечает, но endpoint состояния вернул неожиданный код.",
        }
    return {
        "status": "unavailable",
        "http_status": code,
        "reason": "Сервис ответил ошибкой.",
    }


def _ml_status(settings: Settings) -> tuple[dict[str, Any], str | None]:
    if not settings.ml_enabled:
        return (
            {"status": "disabled", "enabled": False},
            "ML отключён: доступен только BM25, если RAG это поддерживает.",
        )
    url = settings.ml_url.rstrip("/") + "/health"
    return {"enabled": True, "url": url}, None


def _llm_status(settings: Settings) -> tuple[dict[str, Any], str | None]:
    if not settings.llm_enabled:
        return (
            {"status": "disabled", "enabled": False},
            "LLM отключена: доступны поиск, источники и проверенные выдержки.",
        )
    url = settings.llm_url.rstrip("/") + "/models"
    return {"enabled": True, "url": url}, None


async def build_health(settings: Settings, integration: dict[str, Any]) -> dict[str, Any]:
    ml, ml_reason = _ml_status(settings)
    llm, llm_reason = _llm_status(settings)

    probes = []
    if ml.get("enabled"):
        probes.append(_probe(ml["url"]))
    if llm.get("enabled"):
        probes.append(_probe(llm["url"]))
    results = await asyncio.gather(*probes) if probes else []

    index = 0
    if ml.get("enabled"):
        ml.update(results[index])
        index += 1
    if llm.get("enabled"):
        llm.update(results[index])

    # Internal addresses are useful to an operator but should not be exposed as
    # public capabilities; the UI only relies on status and reasons.
    ml.pop("url", None)
    llm.pop("url", None)

    reasons: list[str] = list(integration.get("reasons") or [])
    if ml_reason:
        reasons.append(ml_reason)
    if llm_reason:
        reasons.append(llm_reason)
    if ml.get("status") == "unavailable":
        reasons.append("ML-сервис недоступен: векторный поиск и reranker ограничены.")
    elif ml.get("status") == "degraded":
        reasons.append("ML-сервис отвечает нештатно: режим ограничен.")
    if llm.get("status") == "unavailable":
        reasons.append("LLM недоступна: генерация ответа ограничена.")
    elif llm.get("status") == "degraded":
        reasons.append("LLM отвечает нештатно: режим ограничен.")
    if settings.profile == "development":
        reasons.append("Профиль development не предназначен для рабочего режима.")
    if not settings.threshold_calibrated:
        reasons.append("Пороги релевантности не откалиброваны; оценки поиска не выдаются за вероятность.")

    data_mode = integration.get("data_mode", "unavailable")
    if data_mode in {"degraded", "partial", "unavailable"}:
        application_status = "degraded"
    elif data_mode == "synthetic" or settings.profile == "development":
        application_status = "limited"
    elif ml.get("status") == "unavailable" or llm.get("status") == "unavailable":
        application_status = "degraded"
    elif ml.get("status") == "disabled" or llm.get("status") == "disabled":
        application_status = "limited"
    else:
        application_status = "ok"

    overall = "ok"
    if application_status == "degraded":
        overall = "degraded"
    elif application_status == "limited":
        overall = "limited"

    return {
        "status": overall,
        "profile": settings.profile,
        "application": {
            "status": application_status,
            "data_mode": data_mode,
            "ask": integration.get("ask"),
            "documents": integration.get("documents"),
        },
        "ml": ml,
        "llm": llm,
        "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
    }
