"""Read/write helpers for repos, files and the symbol table (SQLite)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from app.core.models import CodeUnit, ParsedFile, RepoRef, SourceFile
from app.db.connection import transaction


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def repo_id_for(ref: RepoRef) -> str:
    """Readable, URL-safe and case-insensitive id, e.g. `psf__requests`."""
    raw = f"{ref.owner}__{ref.name}".lower()
    return re.sub(r"[^a-z0-9_.-]", "-", raw)


def repo_id_for_slug(slug: str) -> str:
    owner, _, name = slug.partition("/")
    return repo_id_for(RepoRef(owner, name))


def upsert_repo(conn: sqlite3.Connection, ref: RepoRef, pinned_sha: str | None = None) -> str:
    repo_id = repo_id_for(ref)
    conn.execute(
        """INSERT INTO repos (id, url, owner, name, pinned_sha) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET pinned_sha = excluded.pinned_sha,
                                         updated_at = excluded.updated_at""",
        (repo_id, ref.url, ref.owner, ref.name, pinned_sha),
    )
    return repo_id


def get_repo(conn: sqlite3.Connection, repo_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["stats"] = json.loads(out.pop("stats_json") or "{}")
    return out


def list_repos(conn: sqlite3.Connection) -> list[dict]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM repos ORDER BY created_at")]
    return [r for i in ids if (r := get_repo(conn, i)) is not None]


def update_repo(conn: sqlite3.Connection, repo_id: str, **fields) -> None:
    if "stats" in fields:
        fields["stats_json"] = json.dumps(fields.pop("stats"), sort_keys=True, default=str)
    allowed = {"status", "error", "stats_json", "indexed_sha", "pinned_sha", "branch"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Unknown repo fields: {bad}")
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE repos SET {cols} WHERE id = ?", (*fields.values(), repo_id))  # noqa: S608 (keys whitelisted)


def file_hashes(conn: sqlite3.Connection, repo_id: str) -> dict[str, str]:
    rows = conn.execute("SELECT path, content_hash FROM files WHERE repo_id = ?", (repo_id,))
    return {r["path"]: r["content_hash"] for r in rows}


def _symbol_rows(repo_id: str, units: Iterable[CodeUnit]) -> list[tuple]:
    return [
        (
            u.id, repo_id, u.path, u.kind, u.name, u.qualified_name, u.parent, u.start_line,
            u.end_line, u.signature, u.docstring, json.dumps(u.decorators), u.content_hash,
        )
        for u in units
    ]  # fmt: skip


def sync_files_and_symbols(
    conn: sqlite3.Connection,
    repo_id: str,
    sources: list[SourceFile],
    parsed: dict[str, ParsedFile],
    changed_paths: set[str],
    removed_paths: set[str],
) -> None:
    """Apply an (incremental) update: rewrite rows for changed files, delete removed ones."""
    by_path = {s.path: s for s in sources}
    with transaction(conn):
        for path in removed_paths | changed_paths:
            conn.execute("DELETE FROM files WHERE repo_id = ? AND path = ?", (repo_id, path))
        for path in sorted(changed_paths):
            src = by_path[path]
            pf = parsed.get(path)
            if src.language == "python" and pf is not None:
                status, warning = pf.status, pf.warning
            else:
                status, warning = "text_only", None
            conn.execute(
                "INSERT INTO files (repo_id, path, content_hash, language, size_bytes, "
                "parse_status, warning) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, path, src.content_hash, src.language, src.size_bytes, status, warning),
            )
            if pf is not None and pf.units:
                conn.executemany(
                    "INSERT INTO symbols (id, repo_id, path, kind, name, qualified_name, parent, "
                    "start_line, end_line, signature, docstring, decorators_json, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _symbol_rows(repo_id, pf.units),
                )


def find_symbols(conn: sqlite3.Connection, repo_id: str, name: str, limit: int = 20) -> list[dict]:
    """Exact qualified name, or matching short name / dotted suffix."""
    rows = conn.execute(
        "SELECT * FROM symbols WHERE repo_id = ? AND (qualified_name = ? OR name = ? "
        "OR qualified_name LIKE ? ESCAPE '\\') ORDER BY qualified_name LIMIT ?",
        (repo_id, name, name.rsplit(".", 1)[-1], "%." + _like_escape(name), limit),
    ).fetchall()
    out = [dict(r) for r in rows]
    if "." in name:
        out = [
            r
            for r in out
            if r["qualified_name"] == name or r["qualified_name"].endswith("." + name)
        ]
    return out


def symbol_exists(conn: sqlite3.Connection, repo_id: str, name: str) -> bool:
    return bool(find_symbols(conn, repo_id, name, limit=1))


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def count_symbols(conn: sqlite3.Connection, repo_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM symbols WHERE repo_id = ? GROUP BY kind", (repo_id,)
    )
    return {r["kind"]: r["n"] for r in rows}


def snapshot_symbols(conn: sqlite3.Connection, repo_id: str) -> list[tuple]:
    """Deterministic dump of the symbol table (for full-vs-incremental equivalence tests)."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT id, path, kind, qualified_name, start_line, end_line, signature, content_hash "
            "FROM symbols WHERE repo_id = ? ORDER BY id",
            (repo_id,),
        )
    ]
