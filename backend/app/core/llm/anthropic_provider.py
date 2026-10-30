"""Anthropic Messages API provider (official `anthropic` SDK, native tool calling).

Request shape choices (see DECISIONS.md, Phase 2):
  * No `temperature`: the Messages API rejects sampling parameters on every model since
    2026-10-21, so determinism cannot be forced here.
  * `output_config.effort` is always set explicitly (Claude Opus 5.5 defaults to "medium").
  * Top-level `cache_control` auto-caches the growing prefix; agent loops resend the whole
    transcript every step, so this cuts input cost substantially.
  * Refusals: server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) lets the
    API re-run a declined request on Anthropic's recommended fallback model. The served model is
    reported in `LLMResponse.served_model` and used for cost accounting.
  * The assistant's raw content blocks (including thinking blocks) are replayed unchanged.
  * Forced tool choice (`any`/`tool`) is not used: it returns 400 on current models. To force a
    final answer we send `tool_choice: none`.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import anthropic

from app.config import Settings
from app.core.llm.base import (
    CallUsage,
    LLMError,
    LLMResponse,
    Message,
    Stopwatch,
    TextSink,
    ToolCall,
    ToolSpec,
    estimate_cost,
)

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"` on the Claude API.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, model: str, client: Any | None = None) -> None:
        self.model = model
        self.effort = settings.llm_effort
        if client is not None:
            self.client = client
        else:
            key = (
                settings.anthropic_api_key.get_secret_value()
                if settings.anthropic_api_key
                else None
            )
            self.client = anthropic.AsyncAnthropic(
                api_key=key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )

    # -- conversion --------------------------------------------------------------------------
    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict]:
        wire: list[dict] = []
        for m in messages:
            if m.role == "assistant":
                if m.raw is not None and m.raw_provider == "anthropic":
                    wire.append({"role": "assistant", "content": m.raw})
                    continue
                content: list[dict] = []
                if m.text:
                    content.append({"type": "text", "text": m.text})
                for c in m.tool_calls:
                    content.append(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    )
                wire.append({"role": "assistant", "content": content or m.text})
            elif m.tool_results:
                blocks: list[dict] = [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                     **({"is_error": True} if r.is_error else {})}
                    for r in m.tool_results
                ]  # fmt: skip
                if m.text:
                    blocks.append({"type": "text", "text": m.text})
                wire.append({"role": "user", "content": blocks})
            else:
                wire.append({"role": "user", "content": m.text})
        return wire

    @staticmethod
    def _tools(tools: list[ToolSpec] | None) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools or []
        ]

    # -- call --------------------------------------------------------------------------------
    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: Literal["auto", "none"] = "auto",
        on_text: TextSink | None = None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._to_wire(messages),
            "cache_control": {"type": "ephemeral"},
        }
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if tools:
            params["tools"] = self._tools(tools)
            params["tool_choice"] = {"type": tool_choice}
        watch = Stopwatch()
        try:
            fallback = self.model in _FALLBACK_MODELS
            if on_text is not None:
                resp = await self._stream(params, fallback, on_text)
            elif fallback:
                resp = await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **params
                )
            else:
                resp = await self.client.messages.create(**params)
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMError("Anthropic authentication failed (check ANTHROPIC_API_KEY)") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Unknown Anthropic model {self.model!r}") from exc
        except (
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
        ) as exc:
            # The SDK already retried with backoff (max_retries); surface a clean error.
            raise LLMError(f"Anthropic API error after retries: {type(exc).__name__}") from exc
        return self._from_response(resp, watch.elapsed())

    async def _stream(self, params: dict[str, Any], fallback: bool, on_text: TextSink) -> Any:
        """Stream text deltas to `on_text`; return the complete final message."""
        if fallback:
            manager = self.client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks="default", **params
            )
        else:
            manager = self.client.messages.stream(**params)
        async with manager as stream:
            async for text in stream.text_stream:
                on_text(text)
            return await stream.get_final_message()

    def _from_response(self, resp: Any, latency: float) -> LLMResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        served = getattr(resp, "model", None) or self.model
        u = resp.usage
        usage = CallUsage(
            model=served,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            latency_s=latency,
        )
        usage.cost_usd = estimate_cost(self.name, usage)
        stop = {
            "end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
            "refusal": "refusal", "stop_sequence": "end_turn", "pause_turn": "end_turn",
        }.get(resp.stop_reason or "", "end_turn")  # fmt: skip
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            log.warning("Model refused (category=%s)", getattr(details, "category", None))
        text = "".join(text_parts)
        message = Message(
            role="assistant",
            text=text,
            tool_calls=calls,
            raw=list(resp.content),
            raw_provider="anthropic",
        )
        return LLMResponse(
            text=text, tool_calls=calls, stop_reason=stop, usage=usage, message=message,  # type: ignore[arg-type]
            served_model=served if served != self.model else None,
        )  # fmt: skip
