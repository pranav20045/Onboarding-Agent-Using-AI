"""Provider-neutral chat types, usage accounting and the provider interface.

Every provider turns these types into its own wire format and back. Assistant turns keep the
provider's raw content (`Message.raw`) so it can be sent back unchanged on the next request;
Anthropic requires thinking blocks to be replayed exactly as received.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.config import Settings

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]
# Called with each text delta as the model produces it. When given, the provider streams the
# response; the returned LLMResponse is still the complete turn (so callers need not care).
TextSink = Callable[[str], None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_results: list[ToolResult] = field(default_factory=list)  # user only
    raw: Any = None  # provider-native assistant content, replayed verbatim
    raw_provider: str | None = None


@dataclass
class CallUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float | None = None  # None when the model has no entry in the price table


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: CallUsage
    message: Message  # the assistant turn to append to the transcript
    served_model: str | None = None  # differs from the requested model after a fallback


@dataclass
class UsageTotals:
    """Aggregate usage for one answer (all LLM calls + tool calls)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    llm_calls: int = 0
    tool_calls: int = 0
    latency_s: float = 0.0
    models: list[str] = field(default_factory=list)

    def add(self, u: CallUsage) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_tokens += u.cache_read_tokens
        self.cache_write_tokens += u.cache_write_tokens
        self.llm_calls += 1
        if u.cost_usd is None:
            self.cost_known = False
        else:
            self.cost_usd += u.cost_usd
        if u.model not in self.models:
            self.models.append(u.model)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6) if self.cost_known else None,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "latency_s": round(self.latency_s, 3),
            "models": self.models,
        }


# USD per 1M tokens: (input, output, cache_read, cache_write). Edit here when prices change.
# Anthropic rates from the Claude API pricing table (cached 2026-10-06); cache writes are
# billed at 1.25x input for the 5-minute TTL. Ollama runs locally: no API cost.
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
}
FREE_PROVIDERS = {"ollama"}


def estimate_cost(provider: str, usage: CallUsage, prices: dict | None = None) -> float | None:
    if provider in FREE_PROVIDERS:
        return 0.0
    table = prices if prices is not None else PRICES_PER_MTOK
    p = table.get(usage.model)
    if p is None:
        return None
    inp, out, cr, cw = p
    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cr
        + usage.cache_write_tokens * cw
    ) / 1_000_000


class LLMError(RuntimeError):
    """Non-retryable provider failure (bad request, auth, exhausted retries)."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: Literal["auto", "none"] = "auto",
        on_text: TextSink | None = None,
    ) -> LLMResponse: ...


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self.t0


def get_provider(
    settings: Settings, model: str | None = None, provider: str | None = None
) -> LLMProvider:
    """Construct the configured provider. Imports are local so unused SDKs are never loaded."""
    name = provider or settings.llm_provider
    model = model or settings.llm_model
    if name == "anthropic":
        from app.core.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings, model)
    if name in ("openai", "ollama"):
        from app.core.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(settings, model, ollama=name == "ollama")
    raise LLMError(f"Unknown LLM provider {name!r}")
