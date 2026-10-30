"""Index-time history stage: mine all commits, then pre-link the most recent N to PRs/issues."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from app.core.history.git_history import collect_commits, store_commits
from app.core.history.github import GitHubClient
from app.core.history.linking import link_commit, prelink_candidates
from app.core.services import Services
from app.db import store

log = logging.getLogger(__name__)


def github_client_for(svc: Services, repo_id: str) -> GitHubClient | None:
    repo = store.get_repo(svc.conn, repo_id)
    if repo is None or repo["owner"] == "local":
        return None
    token = svc.settings.github_token.get_secret_value() if svc.settings.github_token else None
    return GitHubClient(svc.conn, repo["owner"], repo["name"], token)


async def history_stage(svc: Services, repo_id: str, checkout: Path, progress: Callable[[int, int], None],
                        client: GitHubClient | None = None) -> dict:  # fmt: skip
    rows = await asyncio.to_thread(
        collect_commits, repo_id, checkout
    )  # git work off the event loop
    n_commits = store_commits(svc.conn, repo_id, rows)  # SQLite stays on the caller's thread
    gh = client or github_client_for(svc, repo_id)
    stats = {"commits": n_commits, "prelink_target": 0, "linked": 0, "no_pr": 0, "rate_limited": 0,
             "error": 0, "offline": 0, "github_api_calls": 0}  # fmt: skip
    if gh is None:
        stats["note"] = "no GitHub remote: commit messages only"
        return stats
    try:
        todo = prelink_candidates(svc.conn, repo_id, svc.settings.history_prelink_commits)
        stats["prelink_target"] = len(todo)
        for i, (sha, message) in enumerate(todo, 1):
            status = await link_commit(svc.conn, gh, repo_id, sha, message)
            stats[status] += 1
            progress(i, len(todo))
            if status == "rate_limited":
                stats["rate_limited"] += len(todo) - i  # the rest will be linked lazily later
                log.warning(
                    "GitHub rate limit hit after %d commits; remaining commits link lazily", i
                )
                break
        stats["github_api_calls"] = gh.api_calls
    finally:
        if client is None:
            await gh.aclose()
    return stats
