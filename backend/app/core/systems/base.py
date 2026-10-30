"""The common interface for all three systems (the eval harness only talks to this)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from app.core.agents import verifier
from app.core.llm.base import LLMProvider, Message, UsageTotals
from app.core.repo_context import RepoContext
from app.core.systems.config import SystemConfig

EventSink = Callable[[dict[str, Any]], None]


@dataclass
class SystemAnswer:
    system: str
    answer: str
    citations: list[dict]
    verification: dict
    usage: dict
    trace: list[dict]
    retrieved_files: list[str] = field(default_factory=list)
    retrieved_symbols: list[str] = field(default_factory=list)  # qualified names of retrieved units
    budget_exhausted: bool = False
    error: str | None = None
    config: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class System(Protocol):
    async def answer(self, repo_id: str, question: str, config: SystemConfig,
                     on_event: EventSink | None = None) -> SystemAnswer: ...  # fmt: skip


class Trace:
    """Collects structured events and forwards them to an optional sink (SSE, tracing)."""

    def __init__(self, sink: EventSink | None) -> None:
        self.events: list[dict] = []
        self.sink = sink
        self.t0 = time.perf_counter()

    def emit(self, type_: str, **data: Any) -> None:
        event = {"type": type_, "t": round(time.perf_counter() - self.t0, 3), **data}
        self.events.append(event)
        if self.sink is not None:
            self.sink(event)

    def stream(self, type_: str, **data: Any) -> None:
        """Forward an event to the live sink only. Used for per-token deltas: thousands of them
        would bloat the stored trace (and saved eval runs) without adding information."""
        if self.sink is not None:
            self.sink({"type": type_, "t": round(time.perf_counter() - self.t0, 3), **data})


async def verify_and_revise(
    text: str,
    ctx: RepoContext,
    provider: LLMProvider,
    system_prompt: str,
    transcript: list[Message],
    cfg: SystemConfig,
    usage: UsageTotals,
    trace: Trace,
) -> tuple[str, dict]:
    """Run the citation verifier; if it fails, allow exactly one revision pass."""
    if not cfg.verify:
        return text, {"skipped": True}
    before = verifier.verify(text, ctx)
    trace.emit("verification", ok=before.ok, invalid_citations=len(before.invalid_citations),
               hallucinated_symbols=len(before.hallucinated_symbols))  # fmt: skip
    if before.ok or not cfg.revision_pass:
        return text, verifier.stats(before, None, revised=False)
    prompt = verifier.REVISION_PROMPT.format(problems=before.problems_text())
    msgs = [*transcript, Message(role="user", text=prompt)]
    resp = await provider.chat(system_prompt, msgs, tools=None, max_tokens=cfg.answer_max_tokens)
    usage.add(resp.usage)
    revised = resp.text.strip() or text
    after = verifier.verify(revised, ctx)
    trace.emit("revision", ok=after.ok, invalid_citations=len(after.invalid_citations),
               hallucinated_symbols=len(after.hallucinated_symbols))  # fmt: skip
    return revised, verifier.stats(before, after, revised=True)


def citations_of(text: str) -> list[dict]:
    return [c.as_dict() for c in verifier.parse_citations(text)]
