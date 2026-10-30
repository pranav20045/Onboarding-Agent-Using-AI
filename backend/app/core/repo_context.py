"""Read-only access to one indexed repo: safe file reads, symbol lookup, graph.

All file access by agent tools, the citation verifier and the API's file viewer goes through
`safe_path`, which rejects anything that could escape the checkout.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath

from app.core.graph.query import CodeGraph
from app.core.models import split_lines
from app.db import store

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DENIED_PARTS = {".git"}


class PathError(ValueError):
    pass


def safe_path(root: Path, user_path: str) -> Path:
    """Resolve `user_path` (repo-relative) inside `root`, or raise PathError."""
    if not isinstance(user_path, str) or not user_path.strip():
        raise PathError("path must be a non-empty string")
    if "\x00" in user_path:
        raise PathError("path contains a NUL byte")
    p = user_path.strip().replace("\\", "/")
    if p.startswith("/") or _WINDOWS_DRIVE.match(p) or p.startswith("~"):
        raise PathError("absolute paths are not allowed; use a repo-relative path")
    parts = PurePosixPath(p).parts
    if ".." in parts:
        raise PathError("'..' is not allowed in paths")
    if any(part in DENIED_PARTS for part in parts):
        raise PathError("access to .git is not allowed")
    root_resolved = root.resolve()
    candidate = (root_resolved / p).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PathError("path resolves outside the repository") from exc
    return candidate


@dataclass
class RepoContext:
    repo_id: str
    checkout: Path
    conn: sqlite3.Connection
    graph_path: Path | None = None
    _line_cache: dict[str, list[str]] = field(default_factory=dict)

    @cached_property
    def graph(self) -> CodeGraph | None:
        if self.graph_path is None or not self.graph_path.exists():
            return None
        return CodeGraph.load(self.graph_path)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.checkout.resolve()).as_posix()

    def indexed_paths(self) -> set[str]:
        return set(store.file_hashes(self.conn, self.repo_id))

    def lines(self, path: str) -> list[str]:
        """Lines of a file (only files that are in the index, to honour ignore rules)."""
        target = safe_path(self.checkout, path)
        rel = self.rel(target)
        if rel in self._line_cache:
            return self._line_cache[rel]
        if rel not in self.indexed_paths():
            if target.is_dir():
                raise PathError(f"{rel} is a directory")
            raise FileNotFoundError(f"{rel} is not an indexed file in this repository")
        text = target.read_bytes().decode("utf-8", errors="replace")
        lines = split_lines(text)
        self._line_cache[rel] = lines
        return lines

    def read_range(
        self, path: str, start: int | None = None, end: int | None = None
    ) -> tuple[str, int, int, int]:
        """Return (numbered text, start, end, total_lines) for a 1-based inclusive range."""
        lines = self.lines(path)
        total = len(lines)
        s = max(1, start or 1)
        e = min(total, end or total)
        if total == 0:
            return "", 0, 0, 0
        if s > total:
            raise ValueError(f"start line {s} is beyond end of file ({total} lines)")
        if e < s:
            raise ValueError(f"end line {e} is before start line {s}")
        width = len(str(e))
        body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(s, e + 1))
        return body, s, e, total

    def list_dir(self, path: str = "") -> list[str]:
        """Indexed files and directories directly under `path` (repo-relative)."""
        prefix = ""
        if path.strip() not in ("", ".", "/"):
            safe_path(self.checkout, path)  # validates; raises PathError on escapes
            prefix = path.strip().replace("\\", "/").strip("/") + "/"
        entries: set[str] = set()
        for p in self.indexed_paths():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix) :]
            head, sep, _ = rest.partition("/")
            entries.add(head + "/" if sep else head)
        return sorted(entries)

    @cached_property
    def vocabulary(self) -> set[str]:
        """Every identifier-like token in indexed files (for hallucination checks)."""
        words: set[str] = set()
        for p in self.indexed_paths():
            try:
                text = safe_path(self.checkout, p).read_text(encoding="utf-8", errors="replace")
            except (OSError, PathError):
                continue
            words.update(_IDENT_RE.findall(text))
        return words

    def symbol_known(self, name: str) -> bool:
        return store.symbol_exists(self.conn, self.repo_id, name)
