"""naive_rag: retrieve top-k chunks, one LLM call. The weak baseline.

Defaults for the baseline (set in configs/naive_rag.yaml): fixed-size chunks, dense-only search,
no reranker. Any retrieval setting can be overridden, which makes it reusable for retrieval-only
ablations. Uses the same citation verifier and revision pass as the agents.
"""

from __future__ import annotations

import time

from app.core.agents.prompts import NAIVE_RAG_SYSTEM
from app.core.index.retriever import Retriever
from app.core.llm.base import LLMError, Message, UsageTotals
from app.core.services import Services
from app.core.systems.base import EventSink, SystemAnswer, Trace, citations_of, verify_and_revise
from app.core.systems.config import SystemConfig
from app.db import store


class NaiveRAG:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    async def answer(self, repo_id: str, question: str, config: SystemConfig,
                     on_event: EventSink | None = None) -> SystemAnswer:  # fmt: skip
        cfg = config.resolved(self.svc.settings)
        trace = Trace(on_event)
        usage = UsageTotals()
        t0 = time.perf_counter()
        repo = store.get_repo(self.svc.conn, repo_id) or {"owner": "?", "name": repo_id}
        system_prompt = NAIVE_RAG_SYSTEM.format(repo=f"{repo['owner']}/{repo['name']}")
        provider = self.svc.provider(cfg.llm_provider, cfg.llm_model)
        ctx = self.svc.repo_context(repo_id)

        retriever = Retriever(self.svc.store, repo_id, cfg, self.svc.reranker)
        trace.emit("tool_call", tool="retrieve", args={"query": question, "k": cfg.top_k})
        hits = await retriever.search(question, k=cfg.top_k, scope="all")
        trace.emit("tool_result_summary", tool="retrieve", summary=f"{len(hits)} chunks",
                   files=[h.citation() for h in hits])  # fmt: skip
        context = "\n\n".join(f"{h.text}" for h in hits) or "(no results)"
        user = f"Question: {question}\n\nRetrieved excerpts:\n\n{context}"
        transcript = [Message(role="user", text=user)]
        try:
            resp = await provider.chat(
                system_prompt,
                transcript,
                tools=None,
                max_tokens=cfg.answer_max_tokens,
                on_text=lambda t: trace.stream("answer_delta", text=t),
            )
        except LLMError as exc:
            usage.latency_s = time.perf_counter() - t0
            trace.emit("error", message=str(exc))
            return SystemAnswer("naive_rag", "", [], {}, usage.as_dict(), trace.events,
                                error=str(exc), config=cfg.model_dump())  # fmt: skip
        usage.add(resp.usage)
        text = resp.text.strip()
        trace.emit("turn_end", final=True, chars=len(text))
        transcript.append(resp.message)
        text, verification = await verify_and_revise(
            text, ctx, provider, system_prompt, transcript, cfg, usage, trace
        )
        usage.latency_s = time.perf_counter() - t0
        trace.emit("final", text=text)
        return SystemAnswer(
            system="naive_rag", answer=text, citations=citations_of(text), verification=verification,
            usage=usage.as_dict(), trace=trace.events,
            retrieved_files=list(dict.fromkeys(h.path for h in hits)),
            retrieved_symbols=list(dict.fromkeys(h.qualified_name for h in hits if h.qualified_name)), config=cfg.model_dump(),
        )  # fmt: skip
