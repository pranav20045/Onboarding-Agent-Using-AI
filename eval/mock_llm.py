"""Offline, deterministic stand-ins for the system LLM and the judge.

Used ONLY to prove the eval pipeline end to end without API keys or spend
(`python -m eval.run ... --mock-llm`). Runs made with them are flagged `mock: true` in their
metadata and excluded from summaries; their numbers say nothing about model quality.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from app.core.llm.base import CallUsage, LLMResponse, Message, ToolCall, ToolSpec

_CITE_RE = re.compile(r"\[([A-Za-z0-9_./\-]+):(\d+)-(\d+)\]|^# ([^\s:]+):(\d+)-(\d+)", re.M)
_GREP_RE = re.compile(r"^([^:\n]+):(\d+):", re.M)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STOP = {"what", "where", "when", "does", "how", "with", "from", "that", "this", "which", "there",
         "happens", "defined", "implemented", "function", "class", "method", "repository", "code"}  # fmt: skip


class MockProvider:
    """Agent: one search (or grep), then answers citing the first hit. Naive: cites first excerpt."""

    name = "mock"
    model = "mock-llm"

    def __init__(self, stream_delay: float = 0.0) -> None:
        self.stream_delay = stream_delay  # >0 makes token streaming observable (e2e tests)

    async def chat(self, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                   max_tokens: int = 4096, tool_choice: Literal["auto", "none"] = "auto",
                   on_text=None) -> LLMResponse:  # fmt: skip
        question = messages[0].text
        names = {t.name for t in tools or []}
        n_tool_turns = sum(1 for m in messages if m.tool_results)
        calls: list[ToolCall] = []
        text = ""
        if tools and tool_choice == "auto" and n_tool_turns == 0:
            words = [w for w in _WORD_RE.findall(question) if w.lower() not in _STOP]
            if "search_code" in names:
                calls = [ToolCall("mock_1", "search_code", {"query": question, "k": 5})]
            elif "grep" in names and words:
                calls = [ToolCall("mock_1", "grep", {"pattern": max(words, key=len), "max_results": 20})]
        if not calls:
            context = "\n".join(r.content for m in messages for r in m.tool_results) or question
            m = _CITE_RE.search(context)
            if m:
                path, s, e = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
                text = f"[MOCK ANSWER] The most relevant code is [{path}:{s}-{e}]."
            elif (g := _GREP_RE.search(context)) is not None:
                text = f"[MOCK ANSWER] See [{g.group(1)}:{g.group(2)}-{g.group(2)}]."
            else:
                text = "[MOCK ANSWER] I could not find this in the repository."
        usage = CallUsage(model=self.model, input_tokens=sum(len(x.text) // 4 for x in messages), output_tokens=30,
                          cost_usd=0.0)  # fmt: skip
        msg = Message(role="assistant", text=text, tool_calls=calls)
        if on_text is not None:
            for word in text.split(" "):
                on_text(word + " ")
                if self.stream_delay:
                    await asyncio.sleep(self.stream_delay)
        return LLMResponse(text, calls, "tool_use" if calls else "end_turn", usage, msg)


def mock_judgment(answer: str, gold_files: list[str], category: str) -> dict:
    """Heuristic stand-in for the judge: 1 if a gold file is cited, refusal detection by phrase."""
    refused = "could not find" in answer.lower()
    if category == "unanswerable":
        score = 2 if refused else 0
    else:
        score = 1 if any(f in answer for f in gold_files) else 0
    return {"score": score, "refused": refused, "justification": "mock heuristic (not a real judgment)"}
