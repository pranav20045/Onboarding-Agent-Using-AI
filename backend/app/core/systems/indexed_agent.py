"""indexed_agent: the main system. Agent loop over the pre-built index, code graph and history.

Ablation flags (SystemConfig): chunking, search_mode, rerank, graph_tools, history_tools.
"""

from __future__ import annotations

import time

from app.core.agents.loop import run_agent_loop
from app.core.agents.prompts import INDEXED_AGENT_SYSTEM
from app.core.agents.toolbox import Toolbox
from app.core.agents.tools_common import file_tools
from app.core.agents.tools_indexed import graph_tools, overview_tool, search_tools
from app.core.index.retriever import Retriever
from app.core.llm.base import LLMError, UsageTotals
from app.core.services import Services
from app.core.systems.base import EventSink, SystemAnswer, Trace, citations_of, verify_and_revise
from app.core.systems.config import SystemConfig
from app.db import store


def history_tools_for(svc: Services, repo_id: str, ctx):  # imported lazily: Phase 4 module
    from app.core.history.tools import history_tools

    return history_tools(svc, repo_id, ctx)


class IndexedAgent:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    def build_toolbox(
        self,
        repo_id: str,
        cfg: SystemConfig,
        seen_files: list[str] | None = None,
        seen_symbols: list[str] | None = None,
    ) -> Toolbox:
        ctx = self.svc.repo_context(repo_id)
        retriever = Retriever(self.svc.store, repo_id, cfg, self.svc.reranker)
        tools = [*search_tools(retriever, seen_files, seen_symbols), *file_tools(ctx)]
        graph = ctx.graph if cfg.graph_tools else None
        if graph is not None:
            tools += graph_tools(graph)
        tools.append(overview_tool(ctx, graph))
        if cfg.history_tools:
            tools += history_tools_for(self.svc, repo_id, ctx)
        return Toolbox(tools, cfg.tool_output_max_chars or self.svc.settings.tool_output_max_chars)

    async def answer(self, repo_id: str, question: str, config: SystemConfig,
                     on_event: EventSink | None = None) -> SystemAnswer:  # fmt: skip
        cfg = config.resolved(self.svc.settings)
        trace = Trace(on_event)
        usage = UsageTotals()
        t0 = time.perf_counter()
        repo = store.get_repo(self.svc.conn, repo_id) or {"owner": "?", "name": repo_id}
        clause = (
            ", and inspect git history (blame, commits with linked PRs and issues)"
            if cfg.history_tools
            else ""
        )
        system_prompt = INDEXED_AGENT_SYSTEM.format(
            repo=f"{repo['owner']}/{repo['name']}",
            history_clause=clause,
            max_tool_calls=cfg.max_tool_calls,
        )
        provider = self.svc.provider(cfg.llm_provider, cfg.llm_model)
        ctx = self.svc.repo_context(repo_id)
        seen_files: list[str] = []
        seen_symbols: list[str] = []
        toolbox = self.build_toolbox(repo_id, cfg, seen_files, seen_symbols)
        try:
            result = await run_agent_loop(
                provider, system_prompt, question, toolbox,
                max_tool_calls=cfg.max_tool_calls, max_tokens=cfg.max_tokens,
                answer_max_tokens=cfg.answer_max_tokens, usage=usage, trace=trace,
            )  # fmt: skip
            text, verification = await verify_and_revise(
                result.text, ctx, provider, system_prompt, result.transcript, cfg, usage, trace
            )
        except LLMError as exc:
            usage.latency_s = time.perf_counter() - t0
            trace.emit("error", message=str(exc))
            return SystemAnswer("indexed_agent", "", [], {}, usage.as_dict(), trace.events,
                                error=str(exc), config=cfg.model_dump())  # fmt: skip
        usage.latency_s = time.perf_counter() - t0
        trace.emit("final", text=text, budget_exhausted=result.budget_exhausted)
        retrieved = files_seen(trace.events, seen_files)
        return SystemAnswer(
            system="indexed_agent", answer=text, citations=citations_of(text), verification=verification,
            usage=usage.as_dict(), trace=trace.events, retrieved_files=retrieved, retrieved_symbols=seen_symbols,
            budget_exhausted=result.budget_exhausted,
            error="model refused" if result.stop_reason == "refusal" else None, config=cfg.model_dump(),
        )  # fmt: skip


def files_seen(events: list[dict], search_hits: list[str] | None = None) -> list[str]:
    """Files surfaced by search (in rank order), then files the agent opened (recall@k)."""
    seen: list[str] = list(search_hits or [])
    for e in events:
        if e["type"] == "tool_call" and e["tool"] == "read_file":
            p = e["args"].get("path")
            if p and p not in seen:
                seen.append(p)
    return seen
