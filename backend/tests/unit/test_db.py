from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.connection import (
    current_version,
    discover_migrations,
    init_db,
    migrate,
    transaction,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def test_migrations_apply_and_are_idempotent(conn: sqlite3.Connection) -> None:
    latest = len(discover_migrations())
    assert current_version(conn) == latest
    assert {"repos", "index_jobs", "files", "symbols"} <= _tables(conn)
    assert migrate(conn) == latest  # second run is a no-op


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def _insert_repo(conn: sqlite3.Connection, repo_id: str = "r1") -> None:
    conn.execute(
        "INSERT INTO repos (id, url, owner, name) VALUES (?, ?, ?, ?)",
        (repo_id, f"https://github.com/o/{repo_id}", "o", repo_id),
    )


def test_constraints_enforced(conn: sqlite3.Connection) -> None:
    _insert_repo(conn)
    with pytest.raises(sqlite3.IntegrityError):  # unknown job status
        conn.execute(
            "INSERT INTO index_jobs (id, repo_id, kind, status) VALUES ('j', 'r1', 'full', 'bogus')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # end_line < start_line
        conn.execute(
            "INSERT INTO files (repo_id, path, content_hash, language, size_bytes) "
            "VALUES ('r1', 'a.py', 'h', 'python', 1)"
        )
        conn.execute(
            "INSERT INTO symbols (id, repo_id, path, kind, name, qualified_name, "
            "start_line, end_line, content_hash) "
            "VALUES ('s', 'r1', 'a.py', 'function', 'f', 'a.f', 10, 5, 'h')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # symbol for a file that does not exist
        conn.execute(
            "INSERT INTO symbols (id, repo_id, path, kind, name, qualified_name, "
            "start_line, end_line, content_hash) "
            "VALUES ('s2', 'r1', 'missing.py', 'function', 'f', 'm.f', 1, 2, 'h')"
        )


def test_delete_repo_cascades(conn: sqlite3.Connection) -> None:
    _insert_repo(conn)
    conn.execute(
        "INSERT INTO files (repo_id, path, content_hash, language, size_bytes) "
        "VALUES ('r1', 'a.py', 'h', 'python', 1)"
    )
    conn.execute(
        "INSERT INTO symbols (id, repo_id, path, kind, name, qualified_name, "
        "start_line, end_line, content_hash) "
        "VALUES ('s', 'r1', 'a.py', 'function', 'f', 'a.f', 1, 2, 'h')"
    )
    conn.execute("DELETE FROM repos WHERE id = 'r1'")
    assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_transaction_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        _insert_repo(conn)
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0


def test_failed_migration_is_atomic(tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (mig / "0002_bad.sql").write_text(
        "CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;", encoding="utf-8"
    )
    c = sqlite3.connect(tmp_path / "m.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    with pytest.raises(sqlite3.OperationalError):
        migrate(c, mig)
    assert current_version(c) == 1
    assert "b" not in _tables(c)  # partial 0002 rolled back
    c.close()


def test_migration_naming_validated(tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_gap.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="contiguous"):
        discover_migrations(tmp_path)
    (tmp_path / "bad name.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="Badly named"):
        discover_migrations(tmp_path)
