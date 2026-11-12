"""Deployment contract checks: run without Docker, GPU or model weights."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from navigator.config import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
LOCK = json.loads((ROOT / "config/models.lock.json").read_text(encoding="utf-8"))
PROFILES = ("a100", "3060ti", "4b")


def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)


def test_compose_has_three_services_and_no_model_ports():
    services = COMPOSE["services"]
    assert set(services) == {"app", "embeddings-reranker", "llm"}
    for name in ("embeddings-reranker", "llm"):
        assert "ports" not in services[name], f"{name} must not publish a host port"
        assert services[name].get("expose"), f"{name} needs an internal expose entry"
        assert services[name]["profiles"] == ["models"]
    app_ports = services["app"]["ports"]
    assert any("127.0.0.1" in str(port) for port in app_ports)
    assert "runtime:/data/runtime" in services["app"]["volumes"]
    assert "models:/models:ro" in services["llm"]["volumes"]
    assert "models:/models:ro" in services["embeddings-reranker"]["volumes"]


def test_gpu_overrides_are_explicit():
    a100 = yaml.safe_load((ROOT / "compose.a100.yml").read_text(encoding="utf-8"))
    rtx = yaml.safe_load((ROOT / "compose.3060ti.yml").read_text(encoding="utf-8"))
    assert set(a100["services"]) == {"llm", "embeddings-reranker"}
    assert set(rtx["services"]) == {"llm"}
    for service in a100["services"].values():
        devices = service["deploy"]["resources"]["reservations"]["devices"]
        assert devices[0]["capabilities"] == ["gpu"]


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_env_matches_config_and_lock(profile, monkeypatch):
    _clear_settings_env(monkeypatch)
    path = ROOT / "config/profiles" / f"{profile}.env"
    settings = Settings(_env_file=path)

    expected = {
        "a100": (16384, 8000, 1536, 8, "cuda"),
        "3060ti": (4096, 2200, 768, 4, "cpu"),
        "4b": (4096, 2200, 768, 4, "cpu"),
    }[profile]
    assert (
        settings.context_size,
        settings.context_budget,
        settings.max_output_tokens,
        settings.top_fragments,
        settings.ml_device,
    ) == expected
    assert settings.llm_enabled and settings.ml_enabled
    assert settings.thinking_enabled is False
    assert settings.llm_concurrency == 1
    assert settings.context_budget + settings.max_output_tokens + settings.prompt_reserve_tokens <= settings.context_size

    item = LOCK["profiles"][profile]
    assert settings.llm_model == item["model_alias"]
    assert settings.llm_revision == item["revision"]
    assert item["first_shard"] in item["files"]
    assert settings.embedding_revision == LOCK["ml"]["embedding"]["revision"]
    assert settings.reranker_revision == LOCK["ml"]["reranker"]["revision"]


def test_lock_is_complete_and_pinned():
    assert LOCK["llama_cpp"]["cpu_image"].endswith(
        "@sha256:8a0a31d0b342f96b2c5453eedadbb0544d6d426bd43c9ea6f62b2bd2489be676"
    )
    for profile in PROFILES:
        item = LOCK["profiles"][profile]
        assert len(item["revision"]) == 40
        assert item["files"]
        for filename, digest in item["files"].items():
            assert filename.endswith(".gguf")
            assert len(digest) == 64
    for key in ("embedding", "reranker"):
        item = LOCK["ml"][key]
        assert len(item["revision"]) == 40
        for digest in item["files"].values():
            assert len(digest) == 64


def test_env_example_has_no_secrets_and_safe_defaults():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    assert values["LLM_ENABLED"] == "false"
    assert values["ML_ENABLED"] == "false"
    assert values["APP_BIND"] == "127.0.0.1"
    for forbidden in ("HF_TOKEN", "PASSWORD", "SECRET", "API_KEY", "ACCESS_TOKEN"):
        assert forbidden not in values
    assert not any(str(value).startswith("hf_") for value in values.values())


def test_dockerfile_does_not_bake_weights_or_whole_context():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . " not in dockerfile
    assert "models" not in dockerfile.lower()
    assert "tesseract-ocr-rus" in dockerfile
    assert "tesseract-ocr-eng" in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_deployment_scripts_are_present_and_executable():
    for name in (
        "start.sh",
        "stop.sh",
        "backup.sh",
        "restore.sh",
        "smoke.sh",
        "doctor.sh",
        "tunnel.sh",
        "download-models.sh",
    ):
        path = ROOT / "scripts" / name
        assert path.is_file(), name
        if os.name == "nt":
            # Windows has no Unix executable bit; Git must preserve it for Linux.
            entry = subprocess.check_output(
                ["git", "ls-files", "--stage", "--", f"scripts/{name}"], cwd=ROOT, text=True,
            )
            assert entry.startswith("100755 "), f"{name} must retain its Git executable bit"
        else:
            assert path.stat().st_mode & stat.S_IXUSR, f"{name} must be executable"


def test_backup_and_restore_cover_a_single_runtime_snapshot():
    backup = (ROOT / "scripts/backup.sh").read_text(encoding="utf-8")
    restore = (ROOT / "scripts/restore.sh").read_text(encoding="utf-8")
    assert "stop app" in backup
    assert "wal_checkpoint(TRUNCATE)" in backup
    assert "tar" in backup and "navigator.sqlite" in backup
    assert "sha256sum" in backup
    assert "integrity_check" in restore
    assert "RUNTIME_VOLUME" in backup and "RUNTIME_VOLUME" in restore
