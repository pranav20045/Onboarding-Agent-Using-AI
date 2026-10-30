"""POST /webhooks/github: verified push events trigger incremental re-indexing.

* `X-Hub-Signature-256` is verified with HMAC-SHA256 over the raw body (constant-time compare).
* Replays are rejected by remembering `X-GitHub-Delivery` ids.
* Only pushes to the repo's default branch, for tracked and *unpinned* repos, trigger a job.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.deps import jobs, services
from app.api.errors import APIError
from app.api.jobs import JobManager
from app.core.models import RepoRef
from app.core.services import Services
from app.db import store

router = APIRouter()


def signature_for(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@router.post("/webhooks/github")
async def github_webhook(request: Request, svc: Services = Depends(services),
                         jm: JobManager = Depends(jobs)) -> JSONResponse:  # fmt: skip
    secret = svc.settings.github_webhook_secret
    if secret is None:
        raise APIError(503, "webhook_not_configured", "GITHUB_WEBHOOK_SECRET is not set")
    body = await request.body()
    supplied = request.headers.get("x-hub-signature-256", "")
    expected = signature_for(secret.get_secret_value().encode(), body)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise APIError(401, "invalid_signature", "Webhook signature verification failed")
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    if not delivery or len(delivery) > 100:
        raise APIError(400, "missing_delivery_id", "X-GitHub-Delivery header is required")
    if event == "ping":
        return JSONResponse({"status": "pong"})
    try:
        payload = json.loads(body)
        full_name = payload["repository"]["full_name"]
        default_branch = payload["repository"].get("default_branch") or "main"
        ref = payload.get("ref", "")
    except (ValueError, KeyError, TypeError) as exc:
        raise APIError(400, "invalid_payload", "Malformed webhook payload") from exc
    owner, _, name = str(full_name).partition("/")
    repo_id = store.repo_id_for(RepoRef(owner, name))

    dup = (
        svc.conn.execute(
            "INSERT OR IGNORE INTO webhook_deliveries (delivery_id, repo_id, event, received_at) VALUES (?, ?, ?, ?)",
            (delivery, repo_id, event, store.now_iso()),
        ).rowcount
        == 0
    )
    if dup:
        return JSONResponse({"status": "duplicate_delivery"}, 200)
    if event != "push":
        return JSONResponse({"status": "ignored", "reason": f"event {event!r}"}, 202)
    repo = store.get_repo(svc.conn, repo_id)
    if repo is None:
        raise APIError(404, "repo_not_tracked", f"{full_name} is not indexed here")
    if repo.get("pinned_sha"):
        return JSONResponse(
            {"status": "ignored", "reason": "repository is pinned to a commit"}, 202
        )
    if ref != f"refs/heads/{default_branch}":
        return JSONResponse(
            {"status": "ignored", "reason": f"push to {ref}, tracking {default_branch}"}, 202
        )
    if jm.running(repo_id):
        jm.request_rerun(repo_id)
        return JSONResponse({"status": "queued_after_current_job", "repo_id": repo_id}, 202)
    job_id = jm.start(repo_id, RepoRef(repo["owner"], repo["name"]), None)
    return JSONResponse({"status": "reindex_started", "repo_id": repo_id, "job_id": job_id}, 202)
