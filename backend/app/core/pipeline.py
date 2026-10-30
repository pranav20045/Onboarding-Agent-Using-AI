"""Index pipeline: clone -> walk -> parse -> graph (-> history -> embedding in later stages).

Incremental by design: file content hashes are compared against the previous run, and only
added/changed files are re-parsed (and, later, re-embedded). The code graph is always rebuilt
from the full set of cached parse results, which is cheap and guarantees that an incremental
run produces exactly the same graph as a full one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.core.graph.builder import build_graph
from app.core.graph.query import CodeGraph
from app.core.ingest.walk import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_IGNORE_GLOBS,
    WalkConfig,
    walk_repo,
)
from app.core.models import Chunk, ParsedFile, SourceFile
from app.core.parse.cache import load_parse_cache, save_parse_cache
from app.core.parse.chunking import ChunkConfig, chunks_for_file
from app.core.parse.python_parser import parse_python_file
from app.db import store

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]


def _noop(_stage: str, _done: int, _total: int) -> None:
    pass


@dataclass
class RepoPaths:
    root: Path

    @property
    def checkout(self) -> Path:
        return self.root / "checkout"

    @property
    def graph(self) -> Path:
        return self.root / "graph.json"

    @property
    def parse_cache(self) -> Path:
        return self.root / "parsed.json"

    @classmethod
    def for_repo(cls, settings: Settings, repo_id: str) -> RepoPaths:
        return cls(settings.repos_dir / repo_id)


@dataclass
class ParseOutcome:
    sources: list[SourceFile]
    parsed: dict[str, ParsedFile]
    added: set[str]
    changed: set[str]
    removed: set[str]
    unchanged: set[str]
    skipped: dict[str, str]
    warnings: list[str]
    seconds: float = 0.0

    @property
    def dirty(self) -> set[str]:
        return self.added | self.changed


@dataclass
class IndexStats:
    files_python: int = 0
    files_text: int = 0
    files_skipped: int = 0
    syntax_errors: int = 0
    decode_replaced: int = 0
    units: dict[str, int] = field(default_factory=dict)
    graph: dict = field(default_factory=dict)
    added: int = 0
    changed: int = 0
    removed: int = 0
    timings: dict[str, float] = field(default_factory=dict)


def walk_config(settings: Settings) -> WalkConfig:
    return WalkConfig(
        max_file_size_bytes=settings.max_file_size_bytes,
        max_python_files=settings.max_python_files,
        ignore_dirs=(DEFAULT_IGNORE_DIRS | set(settings.extra_ignore_dirs))
        - set(settings.unignore_dirs),
        ignore_globs=(*DEFAULT_IGNORE_GLOBS, *settings.extra_ignore_globs),
    )


def parse_repo(
    checkout: Path,
    cache_path: Path,
    cfg: WalkConfig,
    previous_hashes: dict[str, str],
    progress: ProgressFn = _noop,
) -> ParseOutcome:
    t0 = time.perf_counter()
    walked = walk_repo(checkout, cfg)
    cache = load_parse_cache(cache_path)
    current = {s.path: s for s in walked.files}
    added = {p for p in current if p not in previous_hashes}
    changed = {
        p
        for p, s in current.items()
        if p in previous_hashes and previous_hashes[p] != s.content_hash
    }
    removed = set(previous_hashes) - set(current)
    unchanged = set(current) - added - changed

    parsed: dict[str, ParsedFile] = {}
    new_cache: dict[str, tuple[str, ParsedFile]] = {}
    py_files = [s for s in walked.files if s.language == "python"]
    for i, src in enumerate(py_files, start=1):
        cached = cache.get(src.path)
        if cached is not None and cached[0] == src.content_hash:
            pf = cached[1]
        else:
            pf = parse_python_file(src.path, src.text, decode_replaced=src.decode_replaced)
            if pf.warning:
                log.warning("%s: %s", src.path, pf.warning)
        parsed[src.path] = pf
        new_cache[src.path] = (src.content_hash, pf)
        if i % 200 == 0 or i == len(py_files):
            progress("parsing", i, len(py_files))
    save_parse_cache(cache_path, new_cache)
    return ParseOutcome(
        sources=walked.files, parsed=parsed, added=added, changed=changed, removed=removed,
        unchanged=unchanged, skipped=walked.skipped, warnings=walked.warnings,
        seconds=time.perf_counter() - t0,
    )  # fmt: skip


def build_chunks(
    outcome: ParseOutcome, paths: set[str] | None, strategy: str, cfg: ChunkConfig
) -> list[Chunk]:
    out: list[Chunk] = []
    for src in outcome.sources:
        if paths is not None and src.path not in paths:
            continue
        out += chunks_for_file(src, outcome.parsed.get(src.path), strategy, cfg)
    return out


async def run_structure_stages(
    conn: sqlite3.Connection,
    settings: Settings,
    repo_id: str,
    checkout: Path,
    paths: RepoPaths,
    progress: ProgressFn = _noop,
    full: bool = False,
) -> tuple[ParseOutcome, CodeGraph, IndexStats]:
    """Parse + symbol table + graph. `full=True` ignores previous hashes and caches.

    CPU-bound work (walking, parsing, graph building) runs in a worker thread so an API process
    stays responsive; SQLite is only touched on the caller's (event-loop) thread.
    """
    loop = asyncio.get_running_loop()

    def threadsafe_progress(stage: str, done: int, total: int) -> None:
        loop.call_soon_threadsafe(progress, stage, done, total)

    previous = {} if full else store.file_hashes(conn, repo_id)
    if full and paths.parse_cache.exists():
        paths.parse_cache.unlink()
    progress("parsing", 0, 0)
    outcome = await asyncio.to_thread(
        parse_repo,
        checkout,
        paths.parse_cache,
        walk_config(settings),
        previous,
        threadsafe_progress,
    )
    if full:
        # Rewrite everything: delete all rows first so stale paths cannot survive.
        conn.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
        outcome.removed = set()
    store.sync_files_and_symbols(
        conn, repo_id, outcome.sources, outcome.parsed,
        changed_paths=outcome.dirty, removed_paths=outcome.removed,
    )  # fmt: skip

    progress("graph", 0, 1)
    t0 = time.perf_counter()

    def _graph() -> CodeGraph:
        graph_nx, res_stats = build_graph(list(outcome.parsed.values()))
        g = CodeGraph(graph_nx, res_stats.as_dict())
        g.save(paths.graph)
        return g

    graph = await asyncio.to_thread(_graph)
    progress("graph", 1, 1)

    stats = IndexStats(
        files_python=sum(1 for s in outcome.sources if s.language == "python"),
        files_text=sum(1 for s in outcome.sources if s.language == "text"),
        files_skipped=len(outcome.skipped),
        syntax_errors=sum(1 for p in outcome.parsed.values() if p.status == "syntax_error"),
        decode_replaced=sum(1 for s in outcome.sources if s.decode_replaced),
        units=store.count_symbols(conn, repo_id),
        graph=graph.summary(),
        added=len(outcome.added), changed=len(outcome.changed), removed=len(outcome.removed),
        timings={"parse_s": round(outcome.seconds, 2), "graph_s": round(time.perf_counter() - t0, 2)},
    )  # fmt: skip
    return outcome, graph, stats
