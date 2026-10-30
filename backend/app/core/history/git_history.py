"""Local git history: commit mining (PyDriller), changed files, blame, log and show.

Every git invocation goes through `run_git` (argument lists, no shell). Paths are validated
with `safe_path` and passed after `--` so they can never be parsed as options.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydriller import Repository

from app.core.gitutil import run_git
from app.core.repo_context import safe_path
from app.db.connection import transaction

SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


@dataclass
class BlameLine:
    line: int
    sha: str
    author: str
    authored_at: int  # unix seconds
    summary: str
    text: str


def collect_commits(repo_id: str, checkout: Path) -> list[tuple]:
    """Every commit reachable from HEAD as DB rows. Touches no database (safe in a worker thread)."""
    files = _changed_files(checkout)
    rows = []
    for c in Repository(str(checkout), only_in_branch=None).traverse_commits():
        rows.append((
            repo_id, c.hash, c.author.name or "", c.author_date.isoformat(), c.msg or "", int(c.merge),
            json.dumps(files.get(c.hash, [])),
        ))  # fmt: skip
    return rows


def mine_commits(conn: sqlite3.Connection, repo_id: str, checkout: Path) -> int:
    return store_commits(conn, repo_id, collect_commits(repo_id, checkout))


def store_commits(conn: sqlite3.Connection, repo_id: str, rows: list[tuple]) -> int:
    """Replace the repo's commit table (sha, author, date, message, changed files)."""
    with transaction(conn):
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.executemany(
            "INSERT INTO commits (repo_id, sha, author, authored_at, message, is_merge, files_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def _changed_files(checkout: Path) -> dict[str, list[str]]:
    """sha -> changed paths, from one `git log` call (PyDriller's per-commit diffs are ~60 ms each)."""
    out = run_git(
        ["log", "--no-renames", "--name-only", "--format=%x00%H", "HEAD"], cwd=checkout, timeout=600
    )
    result: dict[str, list[str]] = {}
    for block in out.split("\x00")[1:]:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if lines:
            result[lines[0]] = lines[1:]
    return result


def resolve_sha(checkout: Path, sha: str) -> str:
    if not SHA_RE.match(sha or ""):
        raise ValueError("sha must be 4-40 hex characters")
    return run_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd=checkout).strip()


def blame(checkout: Path, path: str, start: int, end: int) -> list[BlameLine]:
    target = safe_path(checkout, path)
    rel = target.relative_to(checkout.resolve()).as_posix()
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    out = run_git(
        ["blame", "--porcelain", "-L", f"{start},{end}", "HEAD", "--", rel],
        cwd=checkout,
        timeout=60,
    )
    meta: dict[str, dict] = {}
    lines: list[BlameLine] = []
    current: dict = {}
    for raw in out.splitlines():
        if raw.startswith("\t"):
            sha = current["sha"]
            info = meta.setdefault(sha, {})
            lines.append(BlameLine(current["final"], sha, info.get("author", ""), int(info.get("author-time", 0)),
                                   info.get("summary", ""), raw[1:]))  # fmt: skip
            continue
        parts = raw.split(" ")
        if len(parts) >= 3 and SHA_RE.match(parts[0]) and len(parts[0]) == 40:
            current = {"sha": parts[0], "final": int(parts[2])}
            meta.setdefault(parts[0], {})
        elif current:
            key, _, value = raw.partition(" ")
            meta[current["sha"]][key] = value
    return lines


def git_log(checkout: Path, path: str | None, max_count: int) -> str:
    args = ["log", f"--max-count={max(1, min(max_count, 50))}", "--date=short",
            "--format=%h %ad %an%n    %s", "HEAD"]  # fmt: skip
    if path:
        target = safe_path(checkout, path)
        args += ["--follow", "--", target.relative_to(checkout.resolve()).as_posix()]
    return run_git(args, cwd=checkout, timeout=60)


def git_show(checkout: Path, sha: str, max_diff_chars: int = 6000) -> str:
    full = resolve_sha(checkout, sha)
    header = run_git(["show", "--no-patch", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", full],
                     cwd=checkout)  # fmt: skip
    stat = run_git(["show", "--stat", "--format=", full], cwd=checkout)
    diff = run_git(["show", "--format=", "--unified=2", full], cwd=checkout, timeout=60)
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n[diff truncated at {max_diff_chars} characters]"
    return f"{header.strip()}\n\n{stat.strip()}\n\n{diff}"
