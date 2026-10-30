"""History tools: `git_blame` (shared with grep_agent) and `get_commit` (indexed_agent only)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.agents.toolbox import Tool, schema
from app.core.history.git_history import blame, resolve_sha
from app.core.history.linking import commit_record, link_commit, link_status
from app.core.history.stage import github_client_for
from app.core.llm.base import ToolSpec
from app.core.repo_context import RepoContext
from app.core.services import Services


def blame_tool(ctx: RepoContext) -> Tool:
    async def git_blame(path: str, start: int, end: int) -> str:
        if end - start > 200:
            end = start + 200
        lines = await asyncio.to_thread(blame, ctx.checkout, path, start, end)
        if not lines:
            return "No blame information."
        out: list[str] = []
        group: list = []

        def flush() -> None:
            if group:
                b = group[0]
                when = datetime.fromtimestamp(b.authored_at, UTC).date().isoformat()
                out.append(
                    f"lines {group[0].line}-{group[-1].line}: {b.sha[:10]} {when} {b.author}: {b.summary}"
                )

        for bl in lines:
            if group and bl.sha != group[-1].sha:
                flush()
                group = []
            group.append(bl)
        flush()
        return f"git blame {path}:{start}-{end}\n" + "\n".join(out)

    return Tool(ToolSpec("git_blame", "Which commit last changed each line in a range (grouped by commit). "
                         "Use the commit sha with the commit tools to see why.",
                         schema({"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                                ["path", "start", "end"])),
                git_blame, lambda a, out: f"{max(0, len(out.splitlines()) - 1)} commit groups")  # fmt: skip


def format_commit(rec: dict) -> str:
    parts = [
        f"commit {rec['sha']}\nAuthor: {rec['author']}  Date: {rec['authored_at']}\n\n{rec['message'].strip()}"
    ]
    files = rec.get("files") or []
    if files:
        parts.append(
            "Changed files: " + ", ".join(files[:20]) + (" ..." if len(files) > 20 else "")
        )
    status = rec.get("link_status")
    if rec.get("pull_requests"):
        for pr in rec["pull_requests"]:
            parts.append(
                f"Pull request #{pr['number']}: {pr['title']} ({pr['url']})\n{(pr.get('body') or '').strip()[:2500]}"
            )
            for c in pr.get("review_comments", [])[:5]:
                parts.append(
                    f"  review comment by {c.get('user')} on {c.get('path')}: {c.get('body', '')[:400]}"
                )
            for issue in pr.get("issues", []):
                parts.append(f"Linked issue #{issue['number']}: {issue['title']} ({issue['url']})\n"
                             f"{(issue.get('body') or '').strip()[:1500]}")  # fmt: skip
    elif status == "no_pr":
        parts.append("No pull request is associated with this commit on GitHub.")
    elif status == "rate_limited":
        parts.append("NOTE: GitHub API rate limit reached; PR/issue data unavailable. "
                     "Answer from the commit message alone and say so.")  # fmt: skip
    elif status == "offline":
        parts.append("NOTE: repository has no GitHub remote; only the commit message is available.")
    elif status == "error":
        parts.append("NOTE: GitHub lookup failed; only the commit message is available.")
    return "\n\n".join(parts)


def history_tools(svc: Services, repo_id: str, ctx: RepoContext) -> list[Tool]:
    async def get_commit(sha: str) -> str:
        full = await asyncio.to_thread(resolve_sha, ctx.checkout, sha)
        rec = commit_record(svc.conn, repo_id, full)
        if rec is None:
            return f"Commit {sha} is not in the indexed history."
        if link_status(svc.conn, repo_id, full) in (None, "rate_limited", "error"):
            gh = github_client_for(svc, repo_id)  # lazy link for commits outside the pre-linked set
            try:
                await link_commit(svc.conn, gh, repo_id, full, rec["message"])
            finally:
                if gh is not None:
                    await gh.aclose()
            rec = commit_record(svc.conn, repo_id, full) or rec
        return format_commit(rec)

    return [
        blame_tool(ctx),
        Tool(ToolSpec("get_commit", "A commit's message and changed files plus its pre-linked pull request "
                      "(description, review comments) and linked issues. Use after git_blame for 'why' questions.",
                      schema({"sha": {"type": "string", "description": "Full or abbreviated commit sha"}}, ["sha"])),
             get_commit, lambda a, out: out.splitlines()[0] if out else "(empty)"),
    ]  # fmt: skip
