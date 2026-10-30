"""OpenAI Chat Completions provider, also used for Ollama via its OpenAI-compatible `/v1` API.

Ollama serves open models (e.g. Qwen2.5-Coder) locally with native tool calling, so the same
code path covers both; cost is reported as 0 for Ollama. `temperature` is sent (default 0) for
these providers only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Literal

import openai

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


class OpenAIProvider:
    def __init__(
        self, settings: Settings, model: str, ollama: bool = False, client: Any | None = None
    ) -> None:
        self.name = "ollama" if ollama else "openai"
        self.model = model
        self.temperature = settings.llm_temperature
        if client is not None:
            self.client = client
        elif ollama:
            self.client = openai.AsyncOpenAI(
                base_url=settings.ollama_base_url.rstrip("/") + "/v1", api_key="ollama",
                timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
            )  # fmt: skip
        else:
            key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
            self.client = openai.AsyncOpenAI(
                api_key=key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )

    @staticmethod
    def _to_wire(system: str, messages: list[Message]) -> list[dict]:
        wire: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in m.tool_calls
                    ]  # fmt: skip
                wire.append(entry)
            elif m.tool_results:
                for r in m.tool_results:
                    content = f"ERROR: {r.content}" if r.is_error else r.content
                    wire.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
                if m.text:
                    wire.append({"role": "user", "content": m.text})
            else:
                wire.append({"role": "user", "content": m.text})
        return wire

    async def _stream(self, params: dict[str, Any], on_text: TextSink) -> Any:
        """Stream text deltas to `on_text`; rebuild a complete completion from the chunks.

        Tool-call arguments arrive as JSON fragments keyed by index and are concatenated.
        """
        stream = await self.client.chat.completions.create(
            stream=True, stream_options={"include_usage": True}, **params
        )
        content: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage = None
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                content.append(delta.content)
                on_text(delta.content)
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function is not None:
                    slot["name"] += tc.function.name or ""
                    slot["arguments"] += tc.function.arguments or ""
            if choice.finish_reason:
                finish = choice.finish_reason
        tool_calls = [
            SimpleNamespace(
                id=c["id"] or f"call_{i}",
                function=SimpleNamespace(name=c["name"], arguments=c["arguments"]),
            )
            for i, c in sorted(calls.items())
        ]
        message = SimpleNamespace(content="".join(content) or None, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage
        )

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
            "messages": self._to_wire(system, messages),
            "temperature": self.temperature,
        }
        params["max_tokens" if self.name == "ollama" else "max_completion_tokens"] = max_tokens
        if tools:
            params["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]  # fmt: skip
            params["tool_choice"] = tool_choice
        watch = Stopwatch()
        try:
            if on_text is not None:
                resp = await self._stream(params, on_text)
            else:
                resp = await self.client.chat.completions.create(**params)
        except openai.BadRequestError as exc:
            raise LLMError(f"{self.name} rejected the request: {exc.message}") from exc
        except openai.AuthenticationError as exc:
            raise LLMError(f"{self.name} authentication failed") from exc
        except openai.NotFoundError as exc:
            raise LLMError(f"Unknown {self.name} model {self.model!r}") from exc
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise LLMError(f"{self.name} API error after retries: {type(exc).__name__}") from exc
        latency = watch.elapsed()

        choice = resp.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc.function.arguments}
            calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args if isinstance(args, dict) else {},
                )
            )
        u = resp.usage
        cached = 0
        details = getattr(u, "prompt_tokens_details", None) if u else None
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        usage = CallUsage(
            model=self.model,
            input_tokens=(getattr(u, "prompt_tokens", 0) or 0) - cached if u else 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0 if u else 0,
            cache_read_tokens=cached,
            latency_s=latency,
        )
        usage.cost_usd = estimate_cost(self.name, usage)
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
                "content_filter": "refusal"}.get(choice.finish_reason or "", "end_turn")  # fmt: skip
        if calls:
            stop = "tool_use"
        text = msg.content or ""
        message = Message(role="assistant", text=text, tool_calls=calls)
        return LLMResponse(
            text=text, tool_calls=calls, stop_reason=stop, usage=usage, message=message
        )  # type: ignore[arg-type]
