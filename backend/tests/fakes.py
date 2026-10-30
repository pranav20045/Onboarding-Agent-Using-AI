"""Test doubles: a scripted LLM provider and a ready-to-use in-memory service container."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.core.index.embedder import HashingEmbedder, HashingReranker
from app.core.llm.base import CallUsage, LLMResponse, Message, ToolCall, ToolSpec, estimate_cost
from app.core.services import Services, build_services
from app.db.connection import init_db

_ids = itertools.count(1)


@dataclass
class Step:
    """One scripted model turn: text and/or tool calls ({"name": ..., "args": {...}})."""

    text: str = ""
    calls: list[dict] = field(default_factory=list)
    input_tokens: int = 100
    output_tokens: int = 20


@dataclass
class Recorded:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] | None
    tool_choice: str


class ScriptedProvider:
    """Replays `steps` in order; records every request. Raises if the script runs out."""

    name = "anthropic"

    def __init__(self, steps: list[Step | str], model: str = "claude-opus-5-5") -> None:
        self.model = model
        self.steps = [Step(text=s) if isinstance(s, str) else s for s in steps]
        self.requests: list[Recorded] = []

    async def chat(self, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                   max_tokens: int = 4096, tool_choice: Literal["auto", "none"] = "auto",
                   on_text=None) -> LLMResponse:  # fmt: skip
        self.requests.append(Recorded(system, list(messages), tools, tool_choice))
        if not self.steps:
            raise AssertionError("ScriptedProvider ran out of steps")
        step = self.steps.pop(0)
        calls = [] if tool_choice == "none" or not tools else [
            ToolCall(id=f"call_{next(_ids)}", name=c["name"], arguments=c.get("args", {})) for c in step.calls
        ]  # fmt: skip
        usage = CallUsage(
            model=self.model, input_tokens=step.input_tokens, output_tokens=step.output_tokens
        )
        usage.cost_usd = estimate_cost(self.name, usage)
        msg = Message(role="assistant", text=step.text, tool_calls=calls)
        if on_text is not None:
            self.streamed = getattr(self, "streamed", 0) + 1
            for i in range(0, len(step.text), 7):  # uneven chunks, like real token deltas
                on_text(step.text[i : i + 7])
        return LLMResponse(step.text, calls, "tool_use" if calls else "end_turn", usage, msg)


def make_settings(tmp: Path, **kw) -> Settings:
    return Settings(_env_file=None, data_dir=tmp / "data", qdrant_url=":memory:", **kw)  # type: ignore[call-arg]


def make_services(tmp: Path, provider: ScriptedProvider | None = None, **kw) -> Services:
    return services_for(make_settings(tmp, **kw), provider)


def services_for(settings: Settings, provider: ScriptedProvider | None = None) -> Services:
    return build_services(
        settings,
        conn=init_db(settings.db_path),
        qdrant=AsyncQdrantClient(location=":memory:"),
        embedder=HashingEmbedder(),
        reranker=HashingReranker(),
        provider_overrides={"*": provider} if provider else {},
    )
