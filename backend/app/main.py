"""FastAPI application factory.

uv run uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import ask, repos, webhooks
from app.api.errors import APIError, install_error_handlers
from app.api.jobs import JobManager
from app.api.limits import BodySizeLimit, RateLimiter
from app.config import REPO_ROOT, Settings, get_settings
from app.core.services import Services, build_services

log = logging.getLogger(__name__)
RESULTS_DIR = REPO_ROOT / "eval" / "results"


def create_app(
    settings: Settings | None = None,
    services_factory: Callable[[Settings], Services] = build_services,
    results_dir: Path = RESULTS_DIR,
    clone_overrides: dict[str, str] | None = None,
) -> FastAPI:
    """`services_factory` runs inside the lifespan, i.e. on the server's event-loop thread
    (SQLite connections are bound to the thread that created them). `clone_overrides`
    maps repo ids to local paths and exists for tests only."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.svc = services_factory(settings)
        app.state.jobs = JobManager(
            app.state.svc, settings.max_concurrent_index_jobs, clone_overrides
        )
        recovered = app.state.jobs.recover_interrupted()
        if recovered:
            log.warning("Marked %d interrupted index jobs as failed", recovered)
        app.state.limiter = RateLimiter()
        yield
        await app.state.jobs.shutdown()

    app = FastAPI(title="CodeAtlas", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(BodySizeLimit, default_limit=settings.max_request_bytes,
                       overrides={"/webhooks/github": settings.max_webhook_bytes})  # fmt: skip
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=False,
    )
    app.include_router(repos.router)
    app.include_router(ask.router)
    app.include_router(webhooks.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/results")
    async def results() -> dict:
        summary = results_dir / "summary.json"
        if not summary.exists():
            raise APIError(404, "no_results", "No evaluation results have been published yet")
        return json.loads(summary.read_text(encoding="utf-8"))

    @app.get("/results/charts/{name}")
    async def result_chart(name: str) -> FileResponse:
        """Serve a chart PNG written by `eval.summarize` (whitelisted file names only)."""
        if not re.fullmatch(r"[a-z0-9_]{1,64}\.png", name):
            raise APIError(404, "chart_not_found", "No such chart")
        path = (results_dir / name).resolve()
        if path.parent != results_dir.resolve() or not path.is_file():
            raise APIError(404, "chart_not_found", "No such chart")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()  # services (DB, Qdrant, models) are created in the lifespan, not at import
