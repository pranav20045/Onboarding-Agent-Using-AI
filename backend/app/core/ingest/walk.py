"""Walk a checked-out repo and yield the files worth indexing.

Respects `.gitignore` by asking git for the file list (`git ls-files`), then applies our own
ignore rules (vendored dirs, build output, lock files, binaries, minified/generated files,
size threshold). Symlinks are never followed. Files are read as bytes and decoded as UTF-8,
falling back to replacement characters (with a warning) for anything else.
"""

from __future__ import annotations

import fnmatch
import io
import logging
import os
import tokenize
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.core.gitutil import GitError, run_git
from app.core.models import SourceFile, sha256_hex

log = logging.getLogger(__name__)

DEFAULT_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", ".tox", ".nox", "node_modules",
    "site-packages", "dist-packages", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "build", "dist", ".eggs", "htmlcov", ".idea", ".vscode", "vendor",
    "third_party", "_vendor",
})  # fmt: skip
DEFAULT_IGNORE_GLOBS = (
    "*.min.js", "*.min.css", "*.map", "*_pb2.py", "*_pb2_grpc.py", "*.lock", "poetry.lock",
    "Pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "*.egg-info",
)  # fmt: skip
TEXT_EXTENSIONS = frozenset({
    ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".in",
})  # fmt: skip
TEXT_FILENAMES = frozenset({"Dockerfile", "Makefile", "LICENSE", "CHANGES", "AUTHORS"})


class TooManyFiles(RuntimeError):
    pass


@dataclass
class WalkConfig:
    max_file_size_bytes: int = 1_000_000
    max_python_files: int = 6000
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    extra_ignore_prefixes: tuple[str, ...] = ()  # repo-relative path prefixes to skip


@dataclass
class WalkResult:
    files: list[SourceFile]
    skipped: dict[str, str] = field(default_factory=dict)  # path -> reason
    warnings: list[str] = field(default_factory=list)

    @property
    def python_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.language == "python"]


def classify(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.suffix == ".py" or p.suffix == ".pyi":
        return "python"
    if p.suffix.lower() in TEXT_EXTENSIONS or p.name in TEXT_FILENAMES:
        return "text"
    if p.suffix == "" and p.name.upper().startswith(("README", "CHANGELOG", "CONTRIBUTING")):
        return "text"
    return None


def _candidate_paths(root: Path) -> list[str]:
    """Repo-relative POSIX paths, honoring .gitignore when `root` is a git checkout.

    In a fresh clone every file is tracked, so `.gitignore` only matters for files that were
    committed despite matching it (e.g. force-added build output); those are excluded too.
    """
    if (root / ".git").exists():
        try:
            out = run_git(
                ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
            )
            ignored = run_git(
                ["ls-files", "-z", "--cached", "--ignored", "--exclude-standard"], cwd=root
            )
            skip = {p for p in ignored.split("\0") if p}
            return sorted({p for p in out.split("\0") if p} - skip)
        except GitError as exc:
            log.warning("git ls-files failed (%s); falling back to os.walk", exc)
    paths = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            paths.append(rel)
    return sorted(paths)


def _ignored(rel: str, cfg: WalkConfig) -> str | None:
    parts = PurePosixPath(rel).parts
    for part in parts[:-1]:
        if part in cfg.ignore_dirs or part.endswith(".egg-info"):
            return f"ignored directory {part!r}"
    for prefix in cfg.extra_ignore_prefixes:
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return "ignored prefix"
    name = parts[-1]
    for pattern in cfg.ignore_globs:
        if fnmatch.fnmatch(name, pattern):
            return f"ignored pattern {pattern!r}"
    return None


def is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def decode_source(raw: bytes, is_python: bool) -> tuple[str, bool]:
    """Decode bytes -> (text, replaced). UTF-8 first; Python files may declare another encoding
    with a PEP 263 coding cookie (read with tokenize.detect_encoding, which never executes
    anything); everything else falls back to UTF-8 with replacement characters."""
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        pass
    if is_python:
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            if encoding.lower().replace("_", "-") not in ("utf-8", "utf-8-sig"):
                return raw.decode(encoding), False
        except (SyntaxError, LookupError, UnicodeDecodeError):
            pass
    return raw.decode("utf-8", errors="replace"), True


def walk_repo(root: Path, cfg: WalkConfig | None = None) -> WalkResult:
    cfg = cfg or WalkConfig()
    root = root.resolve()
    result = WalkResult(files=[])
    n_python = 0
    for rel in _candidate_paths(root):
        reason = _ignored(rel, cfg)
        language = classify(rel)
        if reason is None and language is None:
            reason = "unsupported file type"
        if reason:
            result.skipped[rel] = reason
            continue
        abs_path = root / rel
        if abs_path.is_symlink():
            result.skipped[rel] = "symlink (not followed)"
            continue
        if not abs_path.is_file() or not is_within(root, abs_path):
            result.skipped[rel] = "not a regular file inside the repo"
            continue
        size = abs_path.stat().st_size
        if size > cfg.max_file_size_bytes:
            result.skipped[rel] = f"larger than {cfg.max_file_size_bytes} bytes"
            continue
        raw = abs_path.read_bytes()
        if b"\0" in raw[:8192]:
            result.skipped[rel] = "binary content"
            continue
        if language == "python":
            n_python += 1
            if n_python > cfg.max_python_files:
                raise TooManyFiles(f"More than {cfg.max_python_files} Python files")
        text, replaced = decode_source(raw, language == "python")
        if replaced:
            msg = f"{rel}: not valid UTF-8, decoded with replacement characters"
            log.warning(msg)
            result.warnings.append(msg)
        if text.startswith("﻿"):
            text = text[1:]
        result.files.append(
            SourceFile(
                path=rel,
                language=language,  # type: ignore[arg-type]
                size_bytes=size,
                content_hash=sha256_hex(raw),
                text=text,
                decode_replaced=replaced,
            )
        )
    return result
