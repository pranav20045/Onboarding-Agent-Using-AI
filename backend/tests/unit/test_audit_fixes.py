"""Regression tests for the Phase 0 QA audit findings (qa/TEST_REPORT.md)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings, get_settings
from app.db.connection import current_version, discover_migrations, init_db


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_sec02_data_dir_inside_eval_datasets_rejected(isolated_env: Path) -> None:
    for bad in ("eval/datasets", "eval/datasets/cache", str(REPO_ROOT / "eval" / "datasets" / "x")):
        with pytest.raises(ValidationError, match="eval/datasets"):
            _s(data_dir=bad)
    assert _s(data_dir="data").data_dir == (REPO_ROOT / "data").resolve()


def test_f03_relative_paths_anchor_at_repo_root(isolated_env: Path) -> None:
    s = _s(data_dir="somewhere", sqlite_path="x/y.db", models_cache_dir="m")
    assert s.data_dir == (REPO_ROOT / "somewhere").resolve()
    assert s.db_path == (REPO_ROOT / "x" / "y.db").resolve()
    assert s.models_dir == (REPO_ROOT / "m").resolve()


def test_f10_cors_origins_json_or_comma_separated(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    assert _s().cors_origins == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("CORS_ORIGINS", '["https://c.example"]')
    assert _s().cors_origins == ["https://c.example"]


def test_f09_get_settings_is_cached(isolated_env: Path) -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()


def test_f09_in_memory_database() -> None:
    conn = init_db(":memory:")
    assert current_version(conn) == len(discover_migrations())
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"  # WAL is skipped


def test_f04_fixture_rationale_matches_code(fixture_repo: Path) -> None:
    """The flagship 'why' commit must not contradict the retry code it describes."""
    msg = subprocess.run(["git", "log", "-1", "--format=%B", "--grep", "exponential backoff"],
                         cwd=fixture_repo, capture_output=True, text=True, check=True).stdout  # fmt: skip
    assert "0.5s and then 1s" in msg and "2s backoff" not in msg
    payments = (fixture_repo / "shop/payments.py").read_text(encoding="utf-8")
    deco = next(n for n in ast.walk(ast.parse(payments)) if isinstance(n, ast.Call))
    kw = {k.arg: ast.literal_eval(k.value) for k in deco.keywords}
    assert kw == {"times": 3, "backoff": 0.5}
    # utils.retry sleeps between attempts only: times=3 -> 2 sleeps (0.5s, 1.0s)
    sleeps, delay = [], kw["backoff"]
    for attempt in range(kw["times"]):
        if attempt < kw["times"] - 1:
            sleeps.append(delay)
            delay *= 2
    assert sleeps == [0.5, 1.0]
