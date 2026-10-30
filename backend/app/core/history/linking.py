"""Commit -> PR -> issue linking, stored in SQLite (pre-linked at index time, lazily otherwise)."""

from __future__ import annotations

import json
import logging
import sqlite3
from itertools import zip_longest

from app.core.history.github import (
    GitHubClient,
    GitHubError,
    RateLimited,
    issue_refs,
    pr_number_from_message,
)
from app.db.store import now_iso

log = logging.getLogger(__name__)
MAX_ISSUES_PER_PR = 5
MAX_REVIEW_COMMENTS = 10
BODY_CHARS = 4000


def _set_status(
    conn: sqlite3.Connection, repo_id: str, sha: str, status: str, detail: str | None = None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO commit_link_status (repo_id, sha, status, detail, checked_at) VALUES (?, ?, ?, ?, ?)",
        (repo_id, sha, status, detail, now_iso()),
    )


def link_status(conn: sqlite3.Connection, repo_id: str, sha: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM commit_link_status WHERE repo_id = ? AND sha = ?", (repo_id, sha)
    ).fetchone()
    return row["status"] if row else None


async def _store_pr(
    conn: sqlite3.Connection, gh: GitHubClient, repo_id: str, pr: dict, commit_msg: str
) -> None:
    number = int(pr["number"])
    comments = await gh.review_comments(number, MAX_REVIEW_COMMENTS)
    refs = [n for n in issue_refs(f"{pr.get('title', '')}\n{pr.get('body') or ''}\n{commit_msg}", gh.owner, gh.repo)
            if n != number][:MAX_ISSUES_PER_PR]  # fmt: skip
    conn.execute(
        "INSERT OR REPLACE INTO pull_requests (repo_id, number, title, body, url, state, merged_at, "
        "review_comments_json, linked_issues_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (repo_id, number, pr.get("title") or "", (pr.get("body") or "")[:BODY_CHARS], pr.get("html_url") or "",
         pr.get("state"), pr.get("merged_at"),
         json.dumps([{"user": (c.get("user") or {}).get("login"), "path": c.get("path"),
                      "body": (c.get("body") or "")[:800]} for c in comments]),
         json.dumps(refs), now_iso()),
    )  # fmt: skip
    for n in refs:
        exists = conn.execute(
            "SELECT 1 FROM issues WHERE repo_id = ? AND number = ?", (repo_id, n)
        ).fetchone()
        if exists:
            continue
        issue = await gh.issue(n)
        if issue:
            conn.execute(
                "INSERT OR REPLACE INTO issues (repo_id, number, title, body, url, state, is_pull_request, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (repo_id, n, issue.get("title") or "", (issue.get("body") or "")[:BODY_CHARS],
                 issue.get("html_url") or "", issue.get("state"), int("pull_request" in issue), now_iso()),
            )  # fmt: skip


async def link_commit(conn: sqlite3.Connection, gh: GitHubClient | None, repo_id: str, sha: str,
                      message: str) -> str:  # fmt: skip
    """Link one commit. Returns the resulting status. Never raises for API problems."""
    if gh is None:
        _set_status(conn, repo_id, sha, "offline", "repository has no GitHub remote")
        return "offline"
    try:
        prs: list[dict] = []
        number = pr_number_from_message(message)
        if number is not None:
            pr = await gh.pull(number)
            if pr:
                prs = [pr]
                conn.execute(
                    "INSERT OR IGNORE INTO commit_prs VALUES (?, ?, ?, 'message')",
                    (repo_id, sha, number),
                )
        if not prs:
            prs = await gh.prs_for_commit(sha)
            for pr in prs:
                conn.execute(
                    "INSERT OR IGNORE INTO commit_prs VALUES (?, ?, ?, 'api')",
                    (repo_id, sha, int(pr["number"])),
                )
        for pr in prs[:3]:
            await _store_pr(conn, gh, repo_id, pr, message)
        status = "linked" if prs else "no_pr"
        _set_status(conn, repo_id, sha, status)
        return status
    except RateLimited as exc:
        _set_status(conn, repo_id, sha, "rate_limited", str(exc))
        return "rate_limited"
    except GitHubError as exc:
        log.warning("Linking %s failed: %s", sha[:10], exc)
        _set_status(conn, repo_id, sha, "error", str(exc)[:300])
        return "error"


def commit_record(conn: sqlite3.Connection, repo_id: str, sha_prefix: str) -> dict | None:
    rows = conn.execute(
        "SELECT * FROM commits WHERE repo_id = ? AND sha LIKE ? LIMIT 2",
        (repo_id, sha_prefix.lower() + "%"),
    ).fetchall()
    if len(rows) != 1:
        return None
    c = dict(rows[0])
    c["files"] = json.loads(c.pop("files_json"))
    c["link_status"] = link_status(conn, repo_id, c["sha"])
    prs = []
    for pr_row in conn.execute(
        "SELECT p.* FROM commit_prs cp JOIN pull_requests p ON p.repo_id = cp.repo_id AND p.number = cp.pr_number "
        "WHERE cp.repo_id = ? AND cp.sha = ? ORDER BY p.number", (repo_id, c["sha"])):  # fmt: skip
        pr = dict(pr_row)
        pr["review_comments"] = json.loads(pr.pop("review_comments_json"))
        nums = json.loads(pr.pop("linked_issues_json"))
        pr["issues"] = [dict(r) for r in conn.execute(
            f"SELECT number, title, body, url, state FROM issues WHERE repo_id = ? AND number IN ({','.join('?' * len(nums))})",  # noqa: S608
            (repo_id, *nums))] if nums else []  # fmt: skip
        prs.append(pr)
    c["pull_requests"] = prs
    return c


HOT_FILES = 30  # how many of the most-changed Python files drive the "most-changed" half


def prelink_candidates(conn: sqlite3.Connection, repo_id: str, n: int) -> list[tuple[str, str]]:
    """Commits to pre-link: half touching the most-changed files, the rest the most recent.

    BUILD_PROMPT §5.4 asks for "the most-changed or most-recent N commits". Pre-linking only the
    most recent ones would miss the history of the code that churns most (where "why" questions
    concentrate). The two groups are interleaved so that if the GitHub rate limit stops the run
    early, both have been covered. Commits whose lookup already succeeded are skipped.
    Returns (sha, message), newest first within each group.
    """
    rows = conn.execute(
        "SELECT c.sha, c.message, c.files_json FROM commits c LEFT JOIN commit_link_status s "
        "ON s.repo_id = c.repo_id AND s.sha = c.sha "
        "WHERE c.repo_id = ? AND (s.status IS NULL OR s.status IN ('rate_limited', 'error')) "
        "ORDER BY c.authored_at DESC, c.sha",
        (repo_id,),
    ).fetchall()
    if n <= 0 or not rows:
        return []
    touched: list[list[str]] = [
        [f for f in json.loads(r["files_json"]) if f.endswith(".py")] for r in rows
    ]
    # Rank by changes across *all* mined commits, not just the unlinked ones.
    counts: dict[str, int] = {}
    for (files_json,) in conn.execute(
        "SELECT files_json FROM commits WHERE repo_id = ?", (repo_id,)
    ):
        for f in json.loads(files_json):
            if f.endswith(".py"):
                counts[f] = counts.get(f, 0) + 1
    hot = {f for f, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:HOT_FILES]}
    changed = [i for i, files in enumerate(touched) if hot.intersection(files)]
    recent = list(range(len(rows)))
    picked: list[int] = []
    seen: set[int] = set()
    for a, b in zip_longest(changed[: (n + 1) // 2], recent, fillvalue=None):  # alternate groups
        for i in (a, b):
            if i is not None and i not in seen and len(picked) < n:
                seen.add(i)
                picked.append(i)
    return [(rows[i]["sha"], rows[i]["message"]) for i in picked]
