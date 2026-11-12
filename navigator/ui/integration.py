from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from navigator.config import Settings

log = logging.getLogger("navigator.ui.integration")

# The first name is the agreed default. Extra candidates make the integration
# tolerant to the module name chosen by the owner of the feature branch.
_DEFAULT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "ask": (
        "navigator.rag.router",
        "navigator.rag.routes",
        "navigator.rag.api",
        "navigator.rag.http",
    ),
    "documents": (
        "navigator.documents.router",
        "navigator.documents.routes",
        "navigator.documents.api",
        "navigator.documents.http",
        "navigator.ingestion.router",
    ),
}
_ENV_NAMES = {
    "ask": "ASK_ROUTER_MODULE",
    "documents": "DOCUMENTS_ROUTER_MODULE",
}


@dataclass
class RouterResolution:
    key: str
    router: Any | None = None
    module: str | None = None
    mode: str = "unavailable"  # real | synthetic | unavailable
    reason: str = ""
    candidates: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)

    def public(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "module": self.module,
            "reason": self.reason,
        }


def _candidates(key: str) -> tuple[str, ...]:
    override = os.getenv(_ENV_NAMES[key], "").strip()
    if override:
        names = [name.strip() for name in override.split(",") if name.strip()]
        if names:
            return tuple(dict.fromkeys(names))
    return _DEFAULT_CANDIDATES[key]


def _module_missing(exc: ModuleNotFoundError, name: str) -> bool:
    """True only when the candidate itself is absent, not its dependency."""
    missing = exc.name or ""
    return missing == name or name.startswith(f"{missing}.")


def load_feature_router(key: str) -> RouterResolution:
    """Import the router supplied by Klim or Egor without hiding real errors."""
    candidates = _candidates(key)
    errors: list[str] = []
    for name in candidates:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if _module_missing(exc, name):
                errors.append(f"{name}: модуль не найден")
                continue
            reason = f"Не удалось импортировать {name}: отсутствует зависимость {exc.name}."
            log.warning("%s", reason)
            return RouterResolution(
                key=key,
                mode="unavailable",
                reason=reason,
                candidates=candidates,
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001 - report a deployable diagnosis
            reason = f"Ошибка импорта {name}: {type(exc).__name__}."
            log.exception("Cannot import feature router %s", name)
            return RouterResolution(
                key=key,
                mode="unavailable",
                reason=reason,
                candidates=candidates,
                errors=errors,
            )

        router = getattr(module, "router", None) or getattr(module, "api_router", None)
        if router is None:
            errors.append(f"{name}: модуль найден, но не содержит router/api_router")
            continue
        log.info("Using %s router from %s", key, name)
        return RouterResolution(
            key=key,
            router=router,
            module=name,
            mode="real",
            candidates=candidates,
            errors=errors,
        )

    reason = (
        "Router не подключён: "
        + ("; ".join(errors[-2:]) if errors else "подходящий модуль не найден")
    )
    return RouterResolution(
        key=key,
        mode="unavailable",
        reason=reason,
        candidates=candidates,
        errors=errors,
    )


def synthetic_allowed(settings: Settings, explicit: bool | None = None) -> bool:
    """Synthetic fallbacks are a development default, never a silent production mode."""
    if explicit is not None:
        return explicit
    raw = os.getenv("UI_SYNTHETIC_FALLBACK")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on", "force"}
    return settings.profile == "development"


def integration_summary(records: dict[str, RouterResolution]) -> dict[str, Any]:
    modes = {record.mode for record in records.values()}
    if modes == {"real"}:
        data_mode = "real"
    elif modes == {"synthetic"}:
        data_mode = "synthetic"
    elif modes == {"unavailable"}:
        data_mode = "unavailable"
    else:
        data_mode = "partial"

    reasons: list[str] = []
    for label, key in (("вопросы", "ask"), ("документы", "documents")):
        record = records[key]
        if record.mode == "synthetic":
            reasons.append(f"API «{label}» работает на демонстрационной заглушке.")
        elif record.mode == "unavailable":
            reasons.append(f"API «{label}» недоступен: {record.reason}")

    return {
        "data_mode": data_mode,
        "synthetic": "synthetic" in modes,
        "unavailable": "unavailable" in modes,
        "reasons": reasons,
        "ask": records["ask"].public(),
        "documents": records["documents"].public(),
    }
