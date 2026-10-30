"""Independent QA regression tests for Phase 0 (config, SQLite, repo hygiene).

Tests marked ``xfail(strict=True)`` reproduce defects found in the QA audit
(see ``qa/TEST_REPORT.md``). They currently fail as expected; once a defect is fixed the
test XPASSes, strict mode turns that into a failure, and the marker should be removed.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.db import connection as dbconn


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


# --- Positive checks (pass today) ----------------------------------------------------------


def test_env_example_copied_verbatim_is_a_valid_env_file(isolated_env: Path) -> None:
    """README step 1 is `cp .env.example .env`; that file must load without errors."""
    s = Settings(_env_file=REPO_ROOT / ".env.example")  # type: ignore[arg-type]
    assert s.cors_origins == ["http://localhost:3000"]
    assert s.qdrant_api_key is None  # empty values fall back to defaults
    assert s.anthropic_api_key is None


def test_secrets_not_in_json_dump(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "ADMIN_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "LANGFUSE_SECRET_KEY",
        "QDRANT_API_KEY",
    ):
        monkeypatch.setenv(var, f"QA-SECRET-{var}")
    s = _settings()
    dumped = s.model_dump_json() + repr(s) + str(s)
    assert "QA-SECRET" not in dumped


_KEY_PATTERNS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?:pk|sk)-lf-[0-9a-f-]{20,}"
)


def test_no_secret_patterns_in_git_history() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    log = subprocess.run(
        ["git", "log", "-p", "--all", "--no-color"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", "replace")
    assert _KEY_PATTERNS.findall(log) == []


def test_no_env_file_tracked() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    assert not [p for p in tracked if Path(p).name == ".env" or p.endswith((".db", ".pem"))]


def test_no_shell_true_in_application_code() -> None:
    offenders = []
    for path in (REPO_ROOT / "backend" / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"shell\s*=\s*True|os\.system\(|os\.popen\(", text):
            offenders.append(str(path))
    assert offenders == []


# --- Reproduced defects (xfail strict) -----------------------------------------------------


# QA-F02 fixed in Phase 4 (comment/quote-aware statement splitter); marker removed.
def test_migration_with_semicolon_in_comment(tmp_path: Path) -> None:
    (tmp_path / "0001_x.sql").write_text(
        "-- create a; then b\nCREATE TABLE a (x INTEGER);\nCREATE TABLE b (y INTEGER);",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    assert dbconn.migrate(conn, tmp_path) == 1


# QA-F01 fixed (was: migrate() reads user_version outside its transaction (TOCTOU)); marker removed.
def test_migrate_rechecks_version_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic version of the race seen when 2+ processes call init_db() on a new DB.

    Process B finishes all migrations between process A's version check and A's BEGIN.
    """
    db = tmp_path / "race.db"
    conn_a = dbconn.connect(db)
    real_transaction = dbconn.transaction
    fired = False

    def interleaved(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
        nonlocal fired
        if conn is conn_a and not fired:
            fired = True
            conn_b = dbconn.connect(db)
            monkeypatch.setattr(dbconn, "transaction", real_transaction)
            dbconn.migrate(conn_b)
            conn_b.close()
            monkeypatch.setattr(dbconn, "transaction", interleaved)
        return real_transaction(conn)

    monkeypatch.setattr(dbconn, "transaction", interleaved)
    try:
        assert dbconn.migrate(conn_a) == len(dbconn.discover_migrations())
    finally:
        conn_a.close()


# QA-F03 fixed (was: relative DATA_DIR resolves against the CWD, not the repo root); marker removed.
def test_relative_data_dir_is_cwd_independent(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    from_root = _settings(data_dir="./data").data_dir
    monkeypatch.chdir(REPO_ROOT / "backend")
    from_backend = _settings(data_dir="./data").data_dir
    assert from_root == from_backend


# QA-F03 fixed (was: relative SQLITE_PATH is never resolved); marker removed.
def test_relative_sqlite_path_is_resolved(isolated_env: Path) -> None:
    assert _settings(sqlite_path="x.db").db_path.is_absolute()


def test_connect_wal_switch_under_contention_is_reported_not_asserted() -> None:
    """QA-F01b (connect() 'database is locked' on a fresh DB under concurrent open) is
    timing-dependent, so it is reproduced by `qa/scripts/stress_init_db.py`, not here."""
    assert os.path.exists(REPO_ROOT / "qa" / "scripts" / "stress_init_db.py")
