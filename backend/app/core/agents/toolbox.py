"""Tool registry: schema, argument validation, error capture and output truncation.

Both agents use this class, with the same truncation limit, so neither gets an unfair advantage
in how much tool output it can see.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.llm.base import ToolCall, ToolResult, ToolSpec

log = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[str]]
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolInputError(ValueError):
    pass


@dataclass
class Tool:
    spec: ToolSpec
    handler: Handler
    summarize: Callable[[dict, str], str] | None = None  # one-line summary for the UI


def schema(properties: dict[str, dict], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    if "__invalid_json__" in args:
        raise ToolInputError("arguments were not valid JSON")
    props = spec.input_schema.get("properties", {})
    for name in spec.input_schema.get("required", []):
        if name not in args:
            raise ToolInputError(f"missing required argument {name!r}")
    clean: dict[str, Any] = {}
    for name, value in args.items():
        if name not in props:
            raise ToolInputError(f"unknown argument {name!r}; expected one of {sorted(props)}")
        typ = props[name].get("type")
        if value is None:
            continue
        if typ == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)  # models occasionally quote numbers
        expected = _JSON_TYPES.get(typ)
        if expected and (
            not isinstance(value, expected) or (typ == "integer" and isinstance(value, bool))
        ):
            raise ToolInputError(f"argument {name!r} must be of type {typ}")
        clean[name] = value
    return clean


class Toolbox:
    def __init__(self, tools: list[Tool], max_output_chars: int) -> None:
        self.tools = {t.spec.name: t for t in tools}
        self.max_output_chars = max_output_chars

    @property
    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def truncate(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.max_output_chars:
            return text, False
        note = (f"\n[output truncated: showing the first {self.max_output_chars} of {len(text)} characters; "
                "narrow the query, pattern or line range to see more]")  # fmt: skip
        return text[: self.max_output_chars] + note, True

    async def run(self, call: ToolCall) -> tuple[ToolResult, str, bool]:
        """Execute one call. Never raises: errors come back as `is_error` results."""
        tool = self.tools.get(call.name)
        if tool is None:
            msg = f"Unknown tool {call.name!r}. Available tools: {', '.join(sorted(self.tools))}"
            return ToolResult(call.id, msg, is_error=True), msg, False
        try:
            args = validate_args(tool.spec, call.arguments)
            output = await tool.handler(**args)
        except ToolInputError as exc:
            msg = f"Invalid arguments for {call.name}: {exc}"
            return ToolResult(call.id, msg, is_error=True), msg, False
        except Exception as exc:  # tool failures are reported to the model, never crash the loop
            log.info("Tool %s failed: %s", call.name, exc)
            msg = f"{call.name} failed: {type(exc).__name__}: {exc}"
            return ToolResult(call.id, msg, is_error=True), msg, False
        text, truncated = self.truncate(output)
        summary = (
            tool.summarize(call.arguments, output) if tool.summarize else _default_summary(output)
        )
        return ToolResult(call.id, text), summary, truncated


def _default_summary(output: str) -> str:
    first = output.strip().splitlines()[0] if output.strip() else "(empty)"
    return first[:160]


def describe_call(call: ToolCall) -> str:
    """Human-readable step label for the UI, e.g. "Reading app/middleware.py:40-85"."""
    a = call.arguments
    n = call.name
    if n in ("search_code", "search_docs"):
        return f"Searching {'code' if n == 'search_code' else 'docs'}: {a.get('query', '')}"
    if n == "read_file":
        rng = f":{a['start']}-{a.get('end', '')}" if a.get("start") else ""
        return f"Reading {a.get('path', '')}{rng}"
    if n == "grep":
        return f"Grep: {a.get('pattern', '')}" + (
            f" in {a['path_glob']}" if a.get("path_glob") else ""
        )
    if n == "list_directory":
        return f"Listing {a.get('path') or '/'}"
    if n in ("find_definition", "find_callers", "find_callees"):
        return f"{n.replace('_', ' ').capitalize()}: {a.get('symbol', '')}"
    if n == "git_blame":
        return f"Blame {a.get('path', '')}:{a.get('start', '')}-{a.get('end', '')}"
    if n in ("get_commit", "git_show", "github_pr_for_commit"):
        return f"Commit {str(a.get('sha', ''))[:10]}"
    if n == "git_log":
        return f"Git log {a.get('path', '')}"
    return f"{n}({json.dumps(a)[:80]})"
