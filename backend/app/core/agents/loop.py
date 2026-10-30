"""The shared, hand-written tool-calling loop (used by indexed_agent and grep_agent).

    question -> model -> (tool calls -> results)* -> final answer

Stops when the model answers without calling tools, or when a budget is hit:
  * step budget  = max number of tool calls (default 10)
  * token budget = total tokens processed across LLM calls (input + output + cache)
On budget exhaustion the model is asked for a final answer with tools disabled
(`tool_choice: none`) and the result is flagged `budget_exhausted`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.agents.prompts import FORCE_ANSWER
from app.core.agents.toolbox import Toolbox, describe_call
from app.core.llm.base import LLMProvider, Message, ToolResult, UsageTotals
from app.core.systems.base import Trace


@dataclass
class LoopResult:
    text: str
    transcript: list[Message]
    budget_exhausted: bool
    stop_reason: str


async def run_agent_loop(
    provider: LLMProvider,
    system_prompt: str,
    question: str,
    toolbox: Toolbox,
    *,
    max_tool_calls: int,
    max_tokens: int,
    answer_max_tokens: int,
    usage: UsageTotals,
    trace: Trace,
) -> LoopResult:
    messages: list[Message] = [Message(role="user", text=question)]
    calls_used = 0
    exhausted = False
    while True:
        over_tokens = usage.total_tokens >= max_tokens
        force = calls_used >= max_tool_calls or over_tokens
        if force and not exhausted:
            exhausted = True
            reason = "token budget" if over_tokens else "tool-call budget"
            trace.emit(
                "budget_exhausted", reason=reason, tool_calls=calls_used, tokens=usage.total_tokens
            )
            messages[-1].text = (
                messages[-1].text + "\n\n" if messages[-1].text else ""
            ) + FORCE_ANSWER

        # Text streams to the live sink token by token. A turn that ends in tool calls was only
        # narration ("let me search..."); `turn_end` tells the UI whether the text was the answer.
        resp = await provider.chat(
            system_prompt, messages, tools=toolbox.specs, max_tokens=answer_max_tokens,
            tool_choice="none" if force else "auto",
            on_text=lambda t: trace.stream("answer_delta", text=t),
        )  # fmt: skip
        usage.add(resp.usage)
        messages.append(resp.message)
        trace.emit("turn_end", final=bool(force or not resp.tool_calls), chars=len(resp.text))
        if force or not resp.tool_calls:
            return LoopResult(resp.text.strip(), messages, exhausted, resp.stop_reason)

        results: list[ToolResult] = []
        for call in resp.tool_calls:
            if calls_used >= max_tool_calls:
                results.append(
                    ToolResult(call.id, "Not executed: tool-call budget exhausted.", is_error=True)
                )
                continue
            calls_used += 1
            usage.tool_calls += 1
            trace.emit(
                "tool_call",
                tool=call.name,
                args=call.arguments,
                label=describe_call(call),
                step=calls_used,
            )
            result, summary, truncated = await toolbox.run(call)
            trace.emit("tool_result_summary", tool=call.name, summary=summary, is_error=result.is_error,
                       truncated=truncated, chars=len(result.content), step=calls_used)  # fmt: skip
            results.append(result)
        messages.append(Message(role="user", tool_results=results))
