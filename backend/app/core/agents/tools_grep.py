"""grep_agent tools: regex search and git CLI history (no index, no graph).

Search uses ripgrep when installed (Docker image), else Python `re` in a child process.
Safety: argument lists only (never a shell string); ripgrep gets the pattern via `-e` so it
can't be read as an option and `--no-config` ignores user config; globs are validated; the
child process is killed after GREP_TIMEOUT_S. ripgrep's engine is linear-time; Python's `re`
can backtrack catastrophically (ReDoS), which is why it never runs in the server process.
Results are restricted to the same file set the index contains, so both agents see one corpus.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.core.agents.toolbox import Tool, ToolInputError, schema
from app.core.history.git_history import git_log, git_show, resolve_sha
from app.core.history.tools import blame_tool, format_commit
from app.core.llm.base import ToolSpec
from app.core.repo_context import PathError, RepoContext, safe_path
from app.core.services import Services

GREP_TIMEOUT_S = 10
PYGREP = Path(__file__).with_name("pygrep.py")
MAX_PATTERN_LEN = 300
_GLOB_RE = re.compile(r"^[A-Za-z0-9_.*?/\[\]{},!-]+$")


def rg_binary() -> str | None:
    return shutil.which("rg")


def _validate_glob(glob: str) -> str:
    g = glob.strip()
    if not g or len(g) > 200 or not _GLOB_RE.match(g) or ".." in g or g.startswith("/") or ":" in g:
        raise ToolInputError(
            "path_glob must be a relative glob like 'src/**/*.py' (no '..', no absolute paths)"
        )
    return g


def _ripgrep_cmd(rg: str, pattern: str, path_glob: str | None, max_results: int) -> list[str]:
    args = [rg, "--no-config", "--line-number", "--no-heading", "--color=never", "--with-filename",
            "--max-columns=300", "--max-columns-preview", "--max-filesize=1M", "--glob=!.git",
            "--max-count", str(max_results)]  # fmt: skip
    if path_glob:
        args.append(f"--glob={path_glob}")
    return [*args, "-e", pattern, "--", "."]


def _python_grep_cmd(
    ctx: RepoContext, pattern: str, path_glob: str | None, max_results: int
) -> list[str]:
    # -I: isolated mode (no PYTHONPATH, no user site, no cwd on sys.path); pygrep is stdlib-only.
    return [
        sys.executable,
        "-I",
        str(PYGREP),
        str(ctx.checkout),
        pattern,
        path_glob or "",
        str(max_results),
    ]


def run_grep(
    ctx: RepoContext, pattern: str, path_glob: str | None, max_results: int,
    engine: str = "auto", indexed: set[str] | None = None,
) -> str:  # fmt: skip
    """Search indexed files. engine: 'auto' (ripgrep if installed), 'rg' or 'python'."""
    if not pattern or len(pattern) > MAX_PATTERN_LEN or "\n" in pattern or "\x00" in pattern:
        raise ToolInputError(f"pattern must be 1-{MAX_PATTERN_LEN} characters on one line")
    try:
        re.compile(pattern)  # compiling never runs the pattern; matching happens in a child
    except re.error as exc:
        raise ToolInputError(f"invalid regular expression: {exc}") from exc
    if path_glob:
        path_glob = _validate_glob(path_glob)
    max_results = max(1, min(max_results, 200))
    indexed = indexed if indexed is not None else ctx.indexed_paths()
    rg = rg_binary() if engine in ("auto", "rg") else None
    if engine == "rg" and rg is None:
        raise RuntimeError("ripgrep (rg) is not installed on the server")
    if rg:
        args, stdin, cwd = _ripgrep_cmd(rg, pattern, path_glob, max_results), None, ctx.checkout
    else:
        args = _python_grep_cmd(ctx, pattern, path_glob, max_results)
        stdin, cwd = json.dumps(sorted(indexed)).encode(), ctx.checkout
    try:
        proc = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, timeout=GREP_TIMEOUT_S,
                              check=False)  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"grep timed out after {GREP_TIMEOUT_S}s; use a more specific pattern or glob"
        ) from exc
    if proc.returncode not in (0, 1):  # 1 = no matches (ripgrep)
        raise ToolInputError(proc.stderr.decode("utf-8", "replace").strip()[-300:] or "grep failed")
    lines: list[str] = []
    files: set[str] = set()
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        path, sep, rest = raw.partition(":")
        path = path.removeprefix("./").replace("\\", "/")
        if not sep or path not in indexed:
            continue
        files.add(path)
        lines.append(f"{path}:{rest}")
        if len(lines) >= max_results:
            break
    if not lines:
        return f"No matches for /{pattern}/" + (f" in {path_glob}" if path_glob else "")
    more = " (limit reached; refine the pattern)" if len(lines) >= max_results else ""
    return f"{len(lines)} matches in {len(files)} files{more}\n" + "\n".join(lines)


def grep_tools(svc: Services, repo_id: str, ctx: RepoContext, github: bool) -> list[Tool]:
    async def grep(pattern: str, path_glob: str | None = None, max_results: int = 50) -> str:
        indexed = ctx.indexed_paths()  # SQLite read stays on the event-loop thread
        return await asyncio.to_thread(
            run_grep, ctx, pattern, path_glob, max_results, "auto", indexed
        )

    async def git_log_tool(path: str | None = None, max_count: int = 10) -> str:
        if path:
            try:
                safe_path(ctx.checkout, path)
            except PathError as exc:
                raise ToolInputError(str(exc)) from exc
        out = await asyncio.to_thread(git_log, ctx.checkout, path or None, max_count)
        return out or "No commits found."

    async def git_show_tool(sha: str) -> str:
        return await asyncio.to_thread(git_show, ctx.checkout, sha)

    tools = [
        Tool(ToolSpec("grep", "Search file contents with a regular expression (ripgrep syntax). Returns "
                      "path:line:text matches. Optionally restrict files with a glob like 'src/**/*.py'.",
                      schema({"pattern": {"type": "string"},
                              "path_glob": {"type": "string", "description": "e.g. '**/*.py' or 'src/pkg/*'"},
                              "max_results": {"type": "integer", "description": "default 50, max 200"}},
                             ["pattern"])), grep, lambda a, out: out.splitlines()[0] if out else ""),
        Tool(ToolSpec("git_log", "Recent commits (optionally only those touching a file, following renames).",
                      schema({"path": {"type": "string"}, "max_count": {"type": "integer"}}, [])),
             git_log_tool, lambda a, out: f"{out.count(chr(10) + '    ') + (1 if out else 0)} commits"),
        blame_tool(ctx),
        Tool(ToolSpec("git_show", "Show a commit: full message, changed files and (truncated) diff.",
                      schema({"sha": {"type": "string"}}, ["sha"])), git_show_tool,
             lambda a, out: out.splitlines()[0] if out else ""),
    ]  # fmt: skip
    if github:
        tools.append(_github_tool(svc, repo_id, ctx))
    return tools


def _github_tool(svc: Services, repo_id: str, ctx: RepoContext) -> Tool:
    from app.core.history.linking import commit_record, link_commit, link_status
    from app.core.history.stage import github_client_for

    async def github_pr_for_commit(sha: str) -> str:
        full = await asyncio.to_thread(resolve_sha, ctx.checkout, sha)
        rec = commit_record(svc.conn, repo_id, full)
        if rec is None:
            return f"Commit {sha} not found."
        if link_status(svc.conn, repo_id, full) in (None, "rate_limited", "error"):
            gh = github_client_for(svc, repo_id)
            try:
                await link_commit(svc.conn, gh, repo_id, full, rec["message"])
            finally:
                if gh is not None:
                    await gh.aclose()
            rec = commit_record(svc.conn, repo_id, full) or rec
        return format_commit(rec)

    return Tool(ToolSpec("github_pr_for_commit", "Look up the GitHub pull request (description, review "
                         "comments) and linked issues for a commit.", schema({"sha": {"type": "string"}}, ["sha"])),
                github_pr_for_commit, lambda a, out: out.splitlines()[0] if out else "")  # fmt: skip
