"""Index job orchestration: queued -> cloning -> parsing -> graph -> history -> embedding -> ready.

Used by the CLI, the API's background jobs and the GitHub webhook. Every stage updates the
`index_jobs` row (status + progress) so clients can poll or stream it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from app.core.index.vector_store import collection_name
from app.core.ingest.clone import clone_repo
from app.core.models import RepoRef
from app.core.parse.chunking import ChunkConfig
from app.core.pipeline import ParseOutcome, RepoPaths, build_chunks, run_structure_stages
from app.core.services import Services
from app.db import store

log = logging.getLogger(__name__)

# Optional hook for the history stage (Phase 4); signature: (svc, repo_id, checkout, progress)
HistoryStage = Callable[[Services, str, Path, Callable[[int, int], None]], Awaitable[dict]]


def create_job(
    svc: Services, repo_id: str, kind: str = "full", target_sha: str | None = None
) -> str:
    job_id = uuid.uuid4().hex
    svc.conn.execute(
        "INSERT INTO index_jobs (id, repo_id, kind, status, target_sha) VALUES (?, ?, ?, 'queued', ?)",
        (job_id, repo_id, kind, target_sha),
    )
    return job_id


def set_job(svc: Services, job_id: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in {"status", "progress_done", "progress_total", "error",
                                                       "started_at", "finished_at"}}  # fmt: skip
    cols = ", ".join(f"{k} = ?" for k in fields)
    svc.conn.execute(f"UPDATE index_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))  # noqa: S608


def get_job(svc: Services, job_id: str) -> dict | None:
    row = svc.conn.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def latest_job(svc: Services, repo_id: str) -> dict | None:
    row = svc.conn.execute(
        "SELECT * FROM index_jobs WHERE repo_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (repo_id,),
    ).fetchone()
    return dict(row) if row else None


def _manifest_path(paths: RepoPaths, strategy: str) -> Path:
    return paths.root / f"vectors_{strategy}.json"


async def embed_stage(
    svc: Services, repo_id: str, outcome: ParseOutcome, paths: RepoPaths, strategy: str,
    full: bool, progress: Callable[[int, int], None],
) -> dict:  # fmt: skip
    """Bring one collection in sync with the current files. Returns counts."""
    name = collection_name(repo_id, strategy)
    manifest_file = _manifest_path(paths, strategy)
    models = {"dense_model": svc.embedder.dense_model, "sparse_model": svc.embedder.sparse_model}
    manifest: dict = {}
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    exists = await svc.qdrant.collection_exists(name)
    if full or not exists or {k: manifest.get(k) for k in models} != models:
        await svc.store.delete_collection(name)
        manifest = {}
    await svc.store.ensure_collection(name)
    embedded: dict[str, str] = manifest.get("files", {})
    current = {s.path: s.content_hash for s in outcome.sources}
    stale = {p for p, h in embedded.items() if current.get(p) != h}
    todo = {p for p, h in current.items() if embedded.get(p) != h}
    await svc.store.delete_paths(name, stale)
    chunks = build_chunks(outcome, todo, strategy, ChunkConfig())
    await svc.store.upsert_chunks(name, repo_id, strategy, chunks, progress=progress)
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(
        json.dumps({**models, "files": dict(sorted(current.items()))}), encoding="utf-8"
    )
    return {"collection": name, "deleted_files": len(stale), "embedded_files": len(todo),
            "embedded_chunks": len(chunks), "points": await svc.store.count(name)}  # fmt: skip


async def run_index_job(
    svc: Services,
    repo_id: str,
    job_id: str,
    *,
    ref: RepoRef,
    commit_sha: str | None = None,
    clone_url: str | None = None,
    full: bool = False,
    history_stage: HistoryStage | None = None,
) -> dict:
    """Run every stage; on failure the job and repo end in `failed` with the error message."""
    paths = svc.paths(repo_id)
    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    def stage(status: str, done: int = 0, total: int = 0) -> None:
        set_job(svc, job_id, status=status, progress_done=done, progress_total=total)

    set_job(svc, job_id, started_at=store.now_iso())
    store.update_repo(svc.conn, repo_id, status="indexing", error=None)
    try:
        stage("cloning")
        t0 = time.perf_counter()
        token = svc.settings.github_token.get_secret_value() if svc.settings.github_token else None
        sha = await asyncio.to_thread(
            clone_repo,
            ref, paths.checkout, max_size_mb=svc.settings.max_repo_size_mb, commit_sha=commit_sha,
                         github_token=token, clone_url=clone_url)  # fmt: skip
        timings["clone_s"] = time.perf_counter() - t0

        def structure_progress(st: str, done: int, total: int) -> None:
            stage(st, done, total)

        t0 = time.perf_counter()
        outcome, _graph, stats = await run_structure_stages(
            svc.conn, svc.settings, repo_id, paths.checkout, paths, structure_progress, full=full
        )
        timings["structure_s"] = time.perf_counter() - t0

        history: dict = {}
        if history_stage is not None:
            stage("history")
            t0 = time.perf_counter()
            history = await history_stage(
                svc, repo_id, paths.checkout, lambda d, t: stage("history", d, t)
            )
            timings["history_s"] = time.perf_counter() - t0

        vectors: dict[str, dict] = {}
        t0 = time.perf_counter()
        for strategy in svc.settings.index_strategies:
            stage("embedding")
            vectors[strategy] = await embed_stage(
                svc, repo_id, outcome, paths, strategy, full, lambda d, t: stage("embedding", d, t)
            )
        timings["embedding_s"] = time.perf_counter() - t0
        timings["total_s"] = time.perf_counter() - t_start

        summary = asdict(stats)
        summary["timings"].update({k: round(v, 2) for k, v in timings.items()})
        summary.update(vectors=vectors, history=history, warnings=outcome.warnings[:50])
        store.update_repo(svc.conn, repo_id, status="ready", indexed_sha=sha, stats=summary)
        set_job(svc, job_id, status="ready", finished_at=store.now_iso())
        return {"repo_id": repo_id, "sha": sha, **summary}
    except Exception as exc:
        log.error("Index job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
        msg = f"{type(exc).__name__}: {exc}"[:1000]
        set_job(svc, job_id, status="failed", error=msg, finished_at=store.now_iso())
        store.update_repo(svc.conn, repo_id, status="failed", error=msg)
        raise
