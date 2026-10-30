from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings


def make(**env: str) -> Settings:
    # _env_file=None so a developer's real .env never leaks into tests.
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_defaults_match_spec(isolated_env: Path) -> None:
    s = make()
    assert s.max_repo_size_mb == 300
    assert s.max_python_files == 6000
    assert s.max_file_size_bytes == 1_000_000
    assert s.agent_max_tool_calls == 10
    assert s.history_prelink_commits == 500
    assert s.db_path == s.data_dir / "codeatlas.db"
    assert s.repos_dir == s.data_dir / "repos"
    assert not s.langfuse_enabled


def test_env_overrides_and_list_parsing(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_REPO_SIZE_MB", "50")
    monkeypatch.setenv("CORS_ORIGINS", '["https://a.example", "https://b.example"]')
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    s = make()
    assert s.max_repo_size_mb == 50
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert s.llm_provider == "ollama"


def test_invalid_values_rejected(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    with pytest.raises(ValidationError):
        make()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("MAX_REPO_SIZE_MB", "0")
    with pytest.raises(ValidationError):
        make()


def test_secrets_are_masked(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "hook-secret")
    s = make()
    dumped = repr(s) + str(s.model_dump())
    assert "supersecret" not in dumped
    assert "hook-secret" not in dumped
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-supersecret"


def test_langfuse_enabled_only_with_both_keys(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    assert not make().langfuse_enabled
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert make().langfuse_enabled


def test_env_example_lists_every_setting() -> None:
    """Every field the code reads must be documented in .env.example (and vice versa)."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {
        m.group(1).lower() for m in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)
    }
    fields = set(Settings.model_fields)
    assert fields - documented == set(), "missing from .env.example"
    assert documented - fields == set(), "documented but not read by Settings"
