"""Background index jobs: at most one per repo, a global concurrency cap, and follow-up runs
requested while a job is in flight (e.g. a push webhook arriving mid-index)."""

from __future__ import annotations

import asyncio
import logging

from app.core.history.stage import history_stage
from app.core.indexer import create_job, run_index_job, set_job
from app.core.models import RepoRef
from app.core.services import Services
from app.db import store

log = logging.getLogger(__name__)
ACTIVE = ("queued", "cloning", "parsing", "graph", "history", "embedding")


class JobManager:
    def __init__(
        self, svc: Services, max_concurrent: int, clone_url_override: dict[str, str] | None = None
    ) -> None:
        self.svc = svc
        self.tasks: dict[str, asyncio.Task] = {}
        self.rerun: set[str] = set()
        self.sem = asyncio.Semaphore(max_concurrent)
        self.clone_url_override = clone_url_override or {}  # tests: repo_id -> local path

    def recover_interrupted(self) -> int:
        """Jobs left active by a crash/restart can never finish: mark them failed."""
        cur = self.svc.conn.execute(
            f"UPDATE index_jobs SET status = 'failed', error = 'interrupted by server restart', "  # noqa: S608
            f"finished_at = ? WHERE status IN ({','.join('?' * len(ACTIVE))})",
            (store.now_iso(), *ACTIVE),
        )
        self.svc.conn.execute("UPDATE repos SET status = 'failed', error = 'indexing interrupted' "
                              "WHERE status = 'indexing'")  # fmt: skip
        return cur.rowcount

    def running(self, repo_id: str) -> bool:
        t = self.tasks.get(repo_id)
        return t is not None and not t.done()

    def start(self, repo_id: str, ref: RepoRef, commit_sha: str | None, full: bool = False) -> str:
        if self.running(repo_id):
            raise RuntimeError("an index job is already running for this repository")
        kind = "full" if full else "incremental"
        job_id = create_job(self.svc, repo_id, kind, commit_sha)
        store.update_repo(self.svc.conn, repo_id, status="queued", error=None)
        self.tasks[repo_id] = asyncio.create_task(self._run(repo_id, job_id, ref, commit_sha, full))
        return job_id

    def request_rerun(self, repo_id: str) -> None:
        self.rerun.add(repo_id)

    async def _run(
        self, repo_id: str, job_id: str, ref: RepoRef, commit_sha: str | None, full: bool
    ) -> None:
        try:
            async with self.sem:
                await run_index_job(self.svc, repo_id, job_id, ref=ref, commit_sha=commit_sha, full=full,
                                    clone_url=self.clone_url_override.get(repo_id),
                                    history_stage=history_stage)  # fmt: skip
        except asyncio.CancelledError:
            set_job(
                self.svc, job_id, status="failed", error="cancelled", finished_at=store.now_iso()
            )
            store.update_repo(self.svc.conn, repo_id, status="failed", error="indexing cancelled")
            raise
        except Exception:  # already recorded on the job row by run_index_job
            log.info("Index job %s for %s failed", job_id, repo_id)
        finally:
            self.tasks.pop(repo_id, None)
        if repo_id in self.rerun:
            self.rerun.discard(repo_id)
            repo = store.get_repo(self.svc.conn, repo_id)
            if repo:
                self.start(repo_id, ref, repo.get("pinned_sha"))

    async def shutdown(self) -> None:
        for t in list(self.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
