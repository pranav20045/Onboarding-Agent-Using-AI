"""Provider request/response mapping, using stub SDK clients (no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.core.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from app.core.llm.base import (
    CallUsage,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    UsageTotals,
    estimate_cost,
)
from app.core.llm.openai_provider import OpenAIProvider
from tests.fakes import make_settings

TOOLS = [
    ToolSpec(
        "read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}}
    )
]


class _Recorder:
    def __init__(self, response) -> None:
        self.response = response
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _anthropic_response(content, stop="tool_use", model="claude-opus-5-5"):
    usage = NS(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=0,
    )
    return NS(content=content, stop_reason=stop, usage=usage, model=model, stop_details=None)


async def test_anthropic_request_and_parse(tmp_path: Path) -> None:
    content = [
        NS(type="thinking", thinking="", signature="sig"),
        NS(type="text", text="Let me read it."),
        NS(type="tool_use", id="tu_1", name="read_file", input={"path": "a.py"}),
    ]
    rec = _Recorder(_anthropic_response(content))
    client = NS(beta=NS(messages=rec), messages=_Recorder(None))
    p = AnthropicProvider(make_settings(tmp_path), "claude-opus-5-5", client=client)
    history = [
        Message("user", text="q"),
        Message("assistant", text="", tool_calls=[ToolCall("tu_0", "read_file", {"path": "b.py"})]),
        Message("user", tool_results=[ToolResult("tu_0", "boom", is_error=True)]),
    ]
    resp = await p.chat("sys", history, TOOLS, max_tokens=1000)
    kw = rec.kwargs
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "temperature" not in kw  # rejected by the API on every model
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_0", "content": "boom",
                                                "is_error": True}  # fmt: skip
    assert resp.stop_reason == "tool_use" and resp.tool_calls[0].arguments == {"path": "a.py"}
    assert resp.message.raw == content  # thinking block preserved for replay
    # replayed verbatim on the next request
    await p.chat("sys", [*history, resp.message], TOOLS)
    assert rec.kwargs["messages"][-1] == {"role": "assistant", "content": content}
    # cost: 1000*4 + 200*20 + 5000*0.20 per 1M
    assert resp.usage.cost_usd == pytest.approx((4000 + 4000 + 1000) / 1e6)


async def test_anthropic_fallback_model_reported(tmp_path: Path) -> None:
    rec = _Recorder(
        _anthropic_response([NS(type="text", text="ok")], stop="end_turn", model="claude-opus-4-8")
    )
    p = AnthropicProvider(
        make_settings(tmp_path), "claude-opus-5-5", client=NS(beta=NS(messages=rec))
    )
    resp = await p.chat("s", [Message("user", text="q")])
    assert (
        resp.served_model == "claude-opus-4-8" and resp.usage.cost_usd is None
    )  # not in price table
    assert "tools" not in rec.kwargs


async def test_openai_mapping(tmp_path: Path) -> None:
    tc = NS(id="c1", function=NS(name="read_file", arguments='{"path": "x.py"}'))
    resp = NS(
        choices=[NS(message=NS(content=None, tool_calls=[tc]), finish_reason="tool_calls")],
        usage=NS(prompt_tokens=50, completion_tokens=5, prompt_tokens_details=NS(cached_tokens=10)),
    )
    rec = _Recorder(resp)
    p = OpenAIProvider(
        make_settings(tmp_path), "qwen2.5-coder", ollama=True, client=NS(chat=NS(completions=rec))
    )
    history = [
        Message("user", text="q"),
        Message("assistant", tool_calls=[ToolCall("c0", "read_file", {"path": "a"})]),
        Message("user", tool_results=[ToolResult("c0", "data")]),
    ]
    out = await p.chat("sys", history, TOOLS, tool_choice="none")
    kw = rec.kwargs
    assert kw["temperature"] == 0.0 and kw["max_tokens"] == 4096 and kw["tool_choice"] == "none"
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert kw["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "data"}
    assert out.tool_calls[0].arguments == {"path": "x.py"}
    assert (
        out.usage.input_tokens == 40
        and out.usage.cache_read_tokens == 10
        and out.usage.cost_usd == 0.0
    )


def test_usage_totals() -> None:
    t = UsageTotals()
    t.add(CallUsage("claude-opus-5-5", 1_000_000, 0, cost_usd=4.0))
    t.add(CallUsage("other", 10, 10, cost_usd=None))
    d = t.as_dict()
    assert (
        d["llm_calls"] == 2
        and d["cost_usd"] is None
        and d["models"] == ["claude-opus-5-5", "other"]
    )
    assert estimate_cost("anthropic", CallUsage("claude-sonnet-5-5", 1_000_000, 1_000_000)) == 12.0
    assert estimate_cost("ollama", CallUsage("anything", 10, 10)) == 0.0


# --- token streaming -------------------------------------------------------------------------


class _AnthropicStream:
    """Stands in for `client.messages.stream(...)`: an async context manager with text_stream."""

    def __init__(self, deltas: list[str], final) -> None:
        self._deltas, self._final = deltas, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return self._final


class _StreamRecorder:
    def __init__(self, deltas: list[str], final) -> None:
        self.args = (deltas, final)
        self.kwargs: dict | None = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _AnthropicStream(*self.args)


async def test_anthropic_streams_deltas_and_returns_full_turn(tmp_path: Path) -> None:
    final = _anthropic_response([NS(type="text", text="Hello world")], stop="end_turn")
    rec = _StreamRecorder(["Hel", "lo ", "world"], final)
    client = NS(beta=NS(messages=rec), messages=_StreamRecorder(["x"], final))
    p = AnthropicProvider(make_settings(tmp_path), "claude-opus-5-5", client=client)
    got: list[str] = []
    resp = await p.chat("sys", [Message("user", text="q")], on_text=got.append)
    assert got == ["Hel", "lo ", "world"] and resp.text == "Hello world"
    assert rec.kwargs["betas"] == [FALLBACK_BETA] and rec.kwargs["fallbacks"] == "default"
    assert resp.usage.cost_usd is not None and resp.stop_reason == "end_turn"
    # models without the fallback beta use the plain stream endpoint
    plain = _StreamRecorder(
        ["ok"], _anthropic_response([NS(type="text", text="ok")], "end_turn", "claude-haiku-4-5")
    )
    p2 = AnthropicProvider(make_settings(tmp_path), "claude-haiku-4-5", client=NS(messages=plain))
    await p2.chat("s", [Message("user", text="q")], on_text=lambda t: None)
    assert "betas" not in plain.kwargs


def _chunk(content=None, tool_calls=None, finish=None, usage=None):
    delta = NS(content=content, tool_calls=tool_calls)
    choices = [NS(delta=delta, finish_reason=finish)] if (content or tool_calls or finish) else []
    return NS(choices=choices, usage=usage)


class _OpenAIStreamClient:
    def __init__(self, chunks) -> None:
        self.chunks = chunks
        self.kwargs: dict | None = None
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.kwargs = kwargs

        async def gen():
            for c in self.chunks:
                yield c

        return gen()


async def test_openai_stream_reassembles_text_and_fragmented_tool_calls(tmp_path: Path) -> None:
    frag = lambda i, id_=None, name=None, args="": NS(  # noqa: E731
        index=i, id=id_, function=NS(name=name, arguments=args)
    )
    chunks = [
        _chunk(content="Let me "),
        _chunk(content="look."),
        _chunk(tool_calls=[frag(0, "c1", "read_file", '{"pa')]),
        _chunk(tool_calls=[frag(0, None, None, 'th": "a.py"}')]),
        _chunk(tool_calls=[frag(1, "c2", "list_directory", "{}")]),
        _chunk(finish="tool_calls"),
        _chunk(usage=NS(prompt_tokens=30, completion_tokens=9, prompt_tokens_details=None)),
    ]
    client = _OpenAIStreamClient(chunks)
    p = OpenAIProvider(make_settings(tmp_path), "qwen2.5-coder", ollama=True, client=client)
    got: list[str] = []
    out = await p.chat("sys", [Message("user", text="q")], TOOLS, on_text=got.append)
    assert got == ["Let me ", "look."] and out.text == "Let me look."
    assert [(c.id, c.name, c.arguments) for c in out.tool_calls] == [
        ("c1", "read_file", {"path": "a.py"}),
        ("c2", "list_directory", {}),
    ]
    assert (
        out.stop_reason == "tool_use"
        and out.usage.input_tokens == 30
        and out.usage.output_tokens == 9
    )
    assert client.kwargs["stream"] is True and client.kwargs["stream_options"] == {
        "include_usage": True
    }
