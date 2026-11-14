from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    profile: Literal["development", "a100", "3060ti", "4b"] = "development"
    data_dir: Path = Path("runtime")
    ml_url: str = "http://127.0.0.1:8081"
    llm_url: str = "http://127.0.0.1:8082/v1"
    llm_model: str = "Qwen3-4B-GGUF-Q4_K_M"
    llm_revision: str = "UNVERIFIED"
    reranker_revision: str = "UNVERIFIED"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    ml_device: Literal["cpu", "cuda"] = "cpu"
    thinking_enabled: bool = False
    llm_concurrency: int = Field(default=1, ge=1, le=1)
    llm_enabled: bool = False
    ml_enabled: bool = False
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    embedding_dimension: int = Field(default=1024, ge=1024, le=1024)
    context_size: int = Field(default=4096, gt=0)
    context_budget: int = Field(default=2200, gt=0)
    max_output_tokens: int = Field(default=768, gt=0)
    prompt_reserve_tokens: int = Field(default=1024, gt=0)
    top_fragments: int = Field(default=4, ge=1, le=8)
    rerank_threshold: float = 0.0
    request_timeout: float = Field(default=90.0, gt=0)
    max_upload_mb: int = Field(default=40, gt=0)
    job_timeout: int = Field(default=300, gt=0)
    tesseract_cmd: Path | None = None
    tessdata_prefix: Path | None = None
    review_dir: Path = Path("runtime/corpus-review")
    corpus_manifest: Path | None = None
    # A deployment with uncalibrated scores is explicitly marked in every answer.
    threshold_calibrated: bool = False
    # Web login (public deployment). Off for the local loopback server.
    auth_required: bool = False
    public_origin: str = ""
    session_secret: str = ""
    session_hours: int = Field(default=12, ge=1, le=24 * 30)
    admin_session_minutes: int = Field(default=120, ge=5, le=24 * 60)
    # Public demo account: prefilled on the login page, never an administrator.
    demo_login_enabled: bool = True
    demo_username: str = "admin"
    demo_password: str = "qwerty"
    demo_operator_access: bool = False
    # Separate secret administrator account (PBKDF2 hash from scripts/deploy/make_admin_hash.py).
    admin_username: str = ""
    admin_password_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def profile_defaults(cls, values):
        values = dict(values)
        profile = values.get("profile", "development")
        defaults = {
            "a100": dict(llm_model="Qwen2.5-14B-Instruct-GGUF-Q4_K_M", ml_device="cuda",
                         context_size=16384, context_budget=8000, max_output_tokens=1536, top_fragments=8),
            "3060ti": dict(llm_model="Qwen3-8B-GGUF-Q4_K_M"),
            "4b": dict(llm_model="Qwen3-4B-GGUF-Q4_K_M"),
        }
        for key, value in defaults.get(profile, {}).items():
            values.setdefault(key, value)
        return values

    @model_validator(mode="after")
    def valid_budgets(self):
        if self.thinking_enabled:
            raise ValueError("Thinking mode is disabled in all supported profiles")
        if self.context_budget + self.max_output_tokens + self.prompt_reserve_tokens > self.context_size:
            raise ValueError("Sources + output + prompt reserve exceed context size")
        def pinned(revision):
            return bool(revision.strip()) and revision.strip().upper() != "UNVERIFIED"

        if self.llm_enabled and not pinned(self.llm_revision):
            raise ValueError("Pin LLM revision before enabling it")
        if self.ml_enabled and not pinned(self.reranker_revision):
            raise ValueError("Pin reranker revision before enabling ML")
        if self.ml_enabled and not pinned(self.embedding_revision):
            raise ValueError("Pin embedding revision before enabling ML")
        return self

    @property
    def fingerprint(self):
        return f"{self.embedding_model}@{self.embedding_revision}:{self.embedding_dimension}:normalized"


@lru_cache
def get_settings():
    return Settings()
