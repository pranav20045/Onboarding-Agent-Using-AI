"""POST /repos/{id}/ask: Server-Sent Events stream of agent steps, then the full answer.

Event types: tool_call, tool_result_summary, answer_delta, verification, revision,
budget_exhausted, final, result (the complete SystemAnswer without the trace), error, done.
`done` is always the last event, including after errors. If the client disconnects, the
answering task is cancelled so no orphaned work keeps running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from app.api.deps import get_repo_or_404, rate_limit, services
from app.api.errors import APIError
from app.core.services import Services
from app.core.systems.config import SystemConfig
from app.core.systems.registry import get_system
from app.core.tracing import trace_answer

log = logging.getLogger(__name__)
router = APIRouter()

# Presets for the UI's system selector: the baseline uses its own retrieval settings.
SYSTEM_PRESETS: dict[str, dict] = {
    "indexed_agent": {"system": "indexed_agent"},
    "grep_agent": {"system": "grep_agent"},
    "naive_rag": {
        "system": "naive_rag",
        "chunking": "fixed",
        "search_mode": "dense",
        "rerank": False,
    },
}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    system: Literal["indexed_agent", "grep_agent", "naive_rag"] = "indexed_agent"


async def _ready_repo(repo_id: str, svc: Services = Depends(services)) -> dict:
    repo = get_repo_or_404(svc, repo_id)
    if repo["status"] != "ready" and not repo.get("indexed_sha"):
        raise APIError(409, "repo_not_ready", "The repository has not finished indexing")
    return repo


@router.post("/repos/{repo_id}/ask", response_class=EventSourceResponse)
async def ask(repo_id: str, body: AskRequest, request: Request, repo: dict = Depends(_ready_repo),
              svc: Services = Depends(services)) -> AsyncIterator[ServerSentEvent]:  # fmt: skip
    rate_limit(request, "ask", svc.settings.rate_limit_ask_per_minute)
    cfg = SystemConfig(**SYSTEM_PRESETS[body.system])
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def run() -> None:
        with trace_answer(svc.settings, body.system, body.question, {"repo": repo_id}) as span:
            try:
                ans = await get_system(body.system, svc).answer(
                    repo_id, body.question, cfg, queue.put_nowait
                )
                result = ans.as_dict()
                result.pop("trace", None)
                span.finish(output=ans.answer, metadata={"usage": ans.usage, "verification": ans.verification},
                            error=ans.error)  # fmt: skip
                queue.put_nowait({"type": "result", **result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("ask failed for %s", repo_id)
                span.finish(error=type(exc).__name__)
                queue.put_nowait(
                    {"type": "error", "message": "Answering failed due to an internal error"}
                )
            finally:
                queue.put_nowait(None)

    task = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield ServerSentEvent(data=event, event=event["type"])
        yield ServerSentEvent(data={"type": "done"}, event="done")
    finally:
        if not task.done():  # client went away mid-stream
            task.cancel()
