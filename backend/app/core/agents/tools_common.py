"""File tools shared by both agents (identical implementations keep the comparison fair)."""

from __future__ import annotations

from app.core.agents.toolbox import Tool, schema
from app.core.llm.base import ToolSpec
from app.core.repo_context import RepoContext

MAX_LINES_PER_READ = 400


def file_tools(ctx: RepoContext) -> list[Tool]:
    async def read_file(path: str, start: int | None = None, end: int | None = None) -> str:
        s = start or 1
        e = end if end is not None else s + MAX_LINES_PER_READ - 1
        if e - s + 1 > MAX_LINES_PER_READ:
            e = s + MAX_LINES_PER_READ - 1
        body, s, e, total = ctx.read_range(path, s, e)
        more = f" (file has {total} lines; read further with start={e + 1})" if e < total else ""
        return f"{path}:{s}-{e} of {total}{more}\n{body}"

    async def list_directory(path: str = "") -> str:
        entries = ctx.list_dir(path)
        if not entries:
            return f"No indexed files under {path or '/'}"
        return f"{path or '/'} ({len(entries)} entries)\n" + "\n".join(entries)

    return [
        Tool(
            ToolSpec(
                "read_file",
                "Read lines of a repository file. Output lines are prefixed with their 1-based line "
                f"numbers. Reads at most {MAX_LINES_PER_READ} lines per call; omit start/end to read from the top.",
                schema(
                    {
                        "path": {
                            "type": "string",
                            "description": "Repo-relative path, e.g. src/pkg/mod.py",
                        },
                        "start": {
                            "type": "integer",
                            "description": "First line (1-based, inclusive)",
                        },
                        "end": {"type": "integer", "description": "Last line (inclusive)"},
                    },
                    ["path"],
                ),
            ),
            read_file,
            lambda a, out: out.splitlines()[0] if out else "(empty)",
        ),
        Tool(
            ToolSpec(
                "list_directory",
                "List files and sub-directories directly under a repository directory ('' for the root).",
                schema({"path": {"type": "string", "description": "Repo-relative directory"}}, []),
            ),
            list_directory,
            lambda a, out: out.splitlines()[0] if out else "(empty)",
        ),
    ]
