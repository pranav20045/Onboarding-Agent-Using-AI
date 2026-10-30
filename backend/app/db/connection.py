"""SQLite connection handling and a minimal, explicit migration runner.

Migrations are plain SQL files in `app/db/migrations/` named `NNNN_description.sql`.
They are applied in numeric order inside a transaction, and the applied version is tracked
with `PRAGMA user_version`, so there is no hidden ORM state to explain.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas every CodeAtlas connection needs."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(db_path) != ":memory:":
        _enable_wal(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection, deadline_s: float = 30.0) -> None:
    """Switch to WAL, retrying while other processes hold the lock.

    Changing the journal mode needs an exclusive lock and does not wait on `busy_timeout`, so
    several processes opening a brand-new database at once could fail with "database is
    locked" (QA-F01b). WAL mode is persistent, so once any process succeeds it is already on.
    """
    delay = 0.01
    start = time.monotonic()
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() - start > deadline_s:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            raise ValueError(f"Badly named migration file: {path.name}")
        found.append((int(match.group(1)), path))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"Migration versions must be contiguous from 1, got {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply all pending migrations. Returns the resulting schema version.

    The version is re-read *inside* each write transaction (BEGIN IMMEDIATE holds the write
    lock), so concurrent processes starting on a new database cannot both apply the same
    migration (QA-F01: check-then-act race).
    """
    version = current_version(conn)
    for target, path in discover_migrations(migrations_dir):
        if target <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        with transaction(conn):
            version = current_version(conn)
            if target <= version:
                continue  # another process applied it while we waited for the lock
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target:d}")
        version = target
    return version


def _split_statements(sql: str) -> list[str]:
    """Split a migration into statements so they run inside our explicit transaction.

    `sqlite3.Connection.executescript` would issue its own COMMIT first, breaking atomicity.
    `--` comments are dropped and `;` inside quoted strings/identifiers is kept, so comments and
    literals may contain semicolons. (Triggers with BEGIN...END bodies are not supported.)
    """
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = len(sql) if nl == -1 else nl
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
