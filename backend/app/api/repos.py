"""Repository endpoints: add/list/get repos, file viewer, tour."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_repo_or_404, jobs, rate_limit, require_admin, services
from app.api.errors import APIError
from app.api.jobs import JobManager
from app.core.indexer import latest_job
from app.core.ingest.url import InvalidRepoURL, parse_github_url, validate_commit_sha
from app.core.repo_context import PathError
from app.core.services import Services
from app.db import store

router = APIRouter()
MAX_VIEW_LINES = 2000


class AddRepo(BaseModel):
    url: str = Field(max_length=300)
    commit_sha: str | None = Field(default=None, max_length=40)


def _public(repo: dict, svc: Services) -> dict:
    job = latest_job(svc, repo["id"])
    stats = repo.get("stats") or {}
    graph = stats.get("graph") or {}
    return {
        "id": repo["id"], "url": repo["url"], "owner": repo["owner"], "name": repo["name"],
        "status": repo["status"], "error": repo.get("error"), "pinned_sha": repo.get("pinned_sha"),
        "indexed_sha": repo.get("indexed_sha"), "updated_at": repo.get("updated_at"),
        "stats": {
            "files_python": stats.get("files_python"), "files_text": stats.get("files_text"),
            "units": stats.get("units"), "resolution": (graph.get("resolution") or {}).get("resolution_rate"),
            "resolution_non_test": (graph.get("resolution") or {}).get("resolution_rate_non_test"),
            "history": {k: v for k, v in (stats.get("history") or {}).items() if k in ("commits", "linked")},
            "timings": stats.get("timings"),
        } if stats else None,
        "job": {k: job[k] for k in ("id", "kind", "status", "progress_done", "progress_total", "error",
                                    "created_at", "finished_at")} if job else None,
    }  # fmt: skip


@router.post("/repos", status_code=202)
async def add_repo(body: AddRepo, request: Request, svc: Services = Depends(services),
                   jm: JobManager = Depends(jobs)) -> dict:  # fmt: skip
    require_admin(request)
    rate_limit(request, "repos", svc.settings.rate_limit_repos_per_minute)
    try:
        ref = parse_github_url(body.url)
        sha = validate_commit_sha(body.commit_sha) if body.commit_sha else None
    except InvalidRepoURL as exc:
        raise APIError(422, "invalid_repo_url", str(exc)) from exc
    except ValueError as exc:
        raise APIError(422, "invalid_commit_sha", str(exc)) from exc
    repo_id = store.repo_id_for(ref)
    if jm.running(repo_id):
        raise APIError(409, "job_running", "This repository is already being indexed",
                       details={"repo_id": repo_id})  # fmt: skip
    store.upsert_repo(svc.conn, ref, pinned_sha=sha)
    job_id = jm.start(repo_id, ref, sha)
    return {"repo_id": repo_id, "job_id": job_id, "status": "queued"}


@router.get("/repos")
async def list_repos(svc: Services = Depends(services)) -> dict:
    return {"repos": [_public(r, svc) for r in store.list_repos(svc.conn)]}


@router.get("/repos/{repo_id}")
async def get_repo(repo_id: str, svc: Services = Depends(services)) -> dict:
    return _public(get_repo_or_404(svc, repo_id), svc)


@router.get("/repos/{repo_id}/file")
async def get_file(
    repo_id: str,
    path: Annotated[str, Query(min_length=1, max_length=500)],
    start: Annotated[int | None, Query(ge=1)] = None,
    end: Annotated[int | None, Query(ge=1)] = None,
    svc: Services = Depends(services),
) -> dict:
    get_repo_or_404(svc, repo_id)
    ctx = svc.repo_context(repo_id)
    try:
        lines = ctx.lines(path)
    except PathError as exc:
        raise APIError(400, "invalid_path", str(exc)) from exc
    except FileNotFoundError as exc:
        raise APIError(404, "file_not_found", str(exc)) from exc
    total = len(lines)
    s = start or 1
    e = min(end or total, total, s + MAX_VIEW_LINES - 1)
    if total and s > total:
        raise APIError(400, "invalid_range", f"start {s} is beyond end of file ({total} lines)")
    return {"path": path.replace("\\", "/").strip("/"), "start": s, "end": e, "total_lines": total,
            "lines": lines[s - 1 : e] if total else []}  # fmt: skip


@router.get("/repos/{repo_id}/tour")
async def get_tour(repo_id: str, refresh: bool = False, svc: Services = Depends(services)) -> dict:
    from app.core.tour.generator import TourUnavailable, get_or_build_tour

    repo = get_repo_or_404(svc, repo_id)
    if repo["status"] != "ready":
        raise APIError(409, "repo_not_ready", "The repository has not finished indexing")
    try:
        return await get_or_build_tour(svc, repo_id, refresh=refresh)
    except TourUnavailable as exc:
        raise APIError(503, "tour_unavailable", str(exc)) from exc
