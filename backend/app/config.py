"""All CodeAtlas settings, loaded from environment variables and an optional `.env` file.

Secrets are typed as `SecretStr` so they never appear in reprs, logs or error messages.
Every field here must also be listed in `.env.example` (a test enforces this).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[2]

LLMProvider = Literal["anthropic", "openai", "ollama"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # --- Storage -----------------------------------------------------------------------------
    data_dir: Path = Field(
        default=REPO_ROOT / "data",
        description="Root for clones, graphs and the SQLite DB. Never inside eval/datasets.",
    )
    sqlite_path: Path | None = Field(
        default=None, description="Defaults to <data_dir>/codeatlas.db when unset."
    )

    # --- Ingestion limits (BUILD_PROMPT §1, §5.1) ----------------------------------------------
    max_repo_size_mb: int = Field(default=300, gt=0)
    max_python_files: int = Field(default=6000, gt=0)
    max_file_size_bytes: int = Field(default=1_000_000, gt=0)
    # Ignore list (BUILD_PROMPT §5.1): defaults live in app/core/ingest/walk.py; these adjust them.
    extra_ignore_dirs: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="Directory names to skip anywhere in the tree"
    )
    extra_ignore_globs: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="File-name globs to skip, e.g. '*_generated.py'"
    )
    unignore_dirs: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="Default-ignored directory names to index anyway"
    )

    # --- Vector DB / embeddings ----------------------------------------------------------------
    qdrant_url: str = Field(default="http://localhost:6333", description='":memory:" for tests')
    qdrant_api_key: SecretStr | None = None
    dense_embedding_provider: Literal["fastembed", "openai"] = "fastembed"
    dense_embedding_model: str = "jinaai/jina-embeddings-v2-base-code"
    sparse_embedding_model: str = "Qdrant/bm25"
    reranker_model: str = "jinaai/jina-reranker-v1-turbo-en"
    models_cache_dir: Path | None = Field(default=None, description="Defaults to <data_dir>/models")
    index_strategies: list[Literal["ast", "fixed"]] = ["ast", "fixed"]

    # --- LLM providers -----------------------------------------------------------------------
    llm_provider: LLMProvider = "anthropic"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default="medium", description="Anthropic output_config.effort; sent explicitly."
    )
    llm_temperature: float = Field(
        default=0.0,
        ge=0,
        le=2,
        description="OpenAI/Ollama only (Anthropic rejects sampling params).",
    )
    llm_max_retries: int = Field(default=3, ge=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    # The judge is configured separately from the system model (BUILD_PROMPT §6.3).
    judge_provider: LLMProvider = "anthropic"
    judge_model: str = "claude-sonnet-5-5"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"

    # --- Agent budgets (BUILD_PROMPT §5.7) -----------------------------------------------------
    agent_max_tool_calls: int = Field(default=10, gt=0)
    agent_max_tokens: int = Field(default=250_000, gt=0)
    tool_output_max_chars: int = Field(default=12_000, gt=0)

    # --- GitHub / history (BUILD_PROMPT §5.4) ------------------------------------------------
    github_token: SecretStr | None = None
    history_prelink_commits: int = Field(default=500, ge=0)

    # --- API / service (BUILD_PROMPT §5.10) ----------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    admin_token: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    rate_limit_repos_per_minute: int = Field(default=5, ge=1)
    rate_limit_ask_per_minute: int = Field(default=20, ge=1)
    max_request_bytes: int = Field(default=64_000, gt=0)
    max_webhook_bytes: int = Field(default=5_000_000, gt=0)
    max_concurrent_index_jobs: int = Field(default=2, ge=1)

    # --- Observability (optional; tracing is a no-op when unset) -----------------------------
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @field_validator("data_dir", "sqlite_path", "models_cache_dir")
    @classmethod
    def _resolve_path(cls, v: Path | None) -> Path | None:
        """Relative paths are relative to the repo root, not the CWD (QA-F03)."""
        if v is None:
            return None
        v = v.expanduser()
        return (v if v.is_absolute() else REPO_ROOT / v).resolve()

    @field_validator("data_dir")
    @classmethod
    def _not_inside_eval_datasets(cls, v: Path) -> Path:
        """Leakage guard (SEC-02): clones, indexes and caches must never live under the eval
        datasets, so nothing derived from gold answers can be indexed or read by agent tools."""
        datasets = (REPO_ROOT / "eval" / "datasets").resolve()
        if v == datasets or datasets in v.parents:
            raise ValueError("DATA_DIR must not be inside eval/datasets")
        return v

    @field_validator(
        "cors_origins", "extra_ignore_dirs", "extra_ignore_globs", "unignore_dirs", mode="before"
    )
    @classmethod
    def _parse_list(cls, v: object) -> object:
        """Accept a JSON list or a comma-separated string (QA-F10)."""
        if not isinstance(v, str):
            return v
        if v.lstrip().startswith("["):
            return json.loads(v)
        return [o.strip() for o in v.split(",") if o.strip()]

    @property
    def db_path(self) -> Path:
        return self.sqlite_path or (self.data_dir / "codeatlas.db")

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def models_dir(self) -> Path:
        return self.models_cache_dir or (self.data_dir / "models")

    @property
    def langfuse_enabled(self) -> bool:
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
