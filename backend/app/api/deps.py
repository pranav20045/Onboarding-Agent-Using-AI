"""Shared dependencies: services, job manager, admin auth, rate limiting, repo lookup."""

from __future__ import annotations

import hmac

from fastapi import Request

from app.api.errors import APIError
from app.api.jobs import JobManager
from app.api.limits import RateLimiter
from app.core.services import Services
from app.db import store


def services(request: Request) -> Services:
    return request.app.state.svc


def jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int) -> None:
    limiter: RateLimiter = request.app.state.limiter
    wait = limiter.check(bucket, client_ip(request), limit)
    if wait is not None:
        raise APIError(429, "rate_limited", f"Too many requests; retry in {int(wait) + 1} s",
                       headers={"Retry-After": str(int(wait) + 1)})  # fmt: skip


def require_admin(request: Request) -> None:
    token = request.app.state.svc.settings.admin_token
    if token is None:
        return
    header = request.headers.get("authorization", "")
    supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(
        supplied.encode(), token.get_secret_value().encode()
    ):
        raise APIError(401, "unauthorized", "A valid admin token is required to add repositories")


def get_repo_or_404(svc: Services, repo_id: str) -> dict:
    repo = store.get_repo(svc.conn, repo_id)
    if repo is None:
        raise APIError(404, "repo_not_found", f"Unknown repository {repo_id!r}")
    return repo
