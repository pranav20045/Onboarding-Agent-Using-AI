"""grep_agent: explore-on-demand baseline (grep / read / list / git), no index or graph.

Same model, loop, budgets, file tools and output truncation as indexed_agent.
`grep_agent_github=true` adds the GitHub PR lookup tool for the fairer history comparison.
"""

from __future__ import annotations

import time

from app.core.agents.loop import run_agent_loop
from app.core.agents.prompts import GREP_AGENT_SYSTEM
from app.core.agents.toolbox import Toolbox
from app.core.agents.tools_common import file_tools
from app.core.agents.tools_grep import grep_tools
from app.core.llm.base import LLMError, UsageTotals
from app.core.services import Services
from app.core.systems.base import EventSink, SystemAnswer, Trace, citations_of, verify_and_revise
from app.core.systems.config import SystemConfig
from app.core.systems.indexed_agent import files_seen
from app.db import store


class GrepAgent:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    def build_toolbox(self, repo_id: str, cfg: SystemConfig) -> Toolbox:
        ctx = self.svc.repo_context(repo_id)
        tools = [
            *grep_tools(self.svc, repo_id, ctx, github=cfg.grep_agent_github),
            *file_tools(ctx),
        ]
        return Toolbox(tools, cfg.tool_output_max_chars or self.svc.settings.tool_output_max_chars)

    async def answer(self, repo_id: str, question: str, config: SystemConfig,
                     on_event: EventSink | None = None) -> SystemAnswer:  # fmt: skip
        cfg = config.resolved(self.svc.settings)
        trace = Trace(on_event)
        usage = UsageTotals()
        t0 = time.perf_counter()
        repo = store.get_repo(self.svc.conn, repo_id) or {"owner": "?", "name": repo_id}
        clause = (
            ", and look up the GitHub pull request for a commit" if cfg.grep_agent_github else ""
        )
        system_prompt = GREP_AGENT_SYSTEM.format(
            repo=f"{repo['owner']}/{repo['name']}",
            github_clause=clause,
            max_tool_calls=cfg.max_tool_calls,
        )
        provider = self.svc.provider(cfg.llm_provider, cfg.llm_model)
        ctx = self.svc.repo_context(repo_id)
        toolbox = self.build_toolbox(repo_id, cfg)
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
            return SystemAnswer("grep_agent", "", [], {}, usage.as_dict(), trace.events,
                                error=str(exc), config=cfg.model_dump())  # fmt: skip
        usage.latency_s = time.perf_counter() - t0
        trace.emit("final", text=text, budget_exhausted=result.budget_exhausted)
        return SystemAnswer(
            system="grep_agent", answer=text, citations=citations_of(text), verification=verification,
            usage=usage.as_dict(), trace=trace.events, retrieved_files=files_seen(trace.events),
            budget_exhausted=result.budget_exhausted,
            error="model refused" if result.stop_reason == "refusal" else None, config=cfg.model_dump(),
        )  # fmt: skip
