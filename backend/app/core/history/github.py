"""GitHub REST client for commit -> PR -> issue linking, with a SQLite response cache.

* Works without a token (60 requests/hour; a warning is logged once).
* Every 200/404 response is cached in `github_cache`, so repeat lookups never hit the API.
* Rate limits: `X-RateLimit-Remaining`/`Reset` are tracked; when exhausted, or on 403/429 with a
  long retry window, `RateLimited` is raised so callers can degrade (answer from the commit
  message alone and say so). Short `Retry-After` waits (<= 10 s) are honoured once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from typing import Any

import httpx

from app.db.store import now_iso

log = logging.getLogger(__name__)
API = "https://api.github.com"
_warned_no_token = False

ISSUE_REF_RE = re.compile(
    r"(?:(?P<kw>close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+)?"
    r"(?:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+))?#(?P<num>\d{1,7})\b",
    re.IGNORECASE,
)
ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<num>\d+)"
)
SQUASH_PR_RE = re.compile(r"\(#(\d{1,7})\)\s*$", re.MULTILINE)
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d{1,7})\b")


class RateLimited(RuntimeError):
    def __init__(self, reset_at: float | None) -> None:
        self.reset_at = reset_at
        wait = f" (resets in {max(0, int(reset_at - time.time()))} s)" if reset_at else ""
        super().__init__(f"GitHub API rate limit reached{wait}")


class GitHubError(RuntimeError):
    pass


def issue_refs(text: str, owner: str, repo: str) -> list[int]:
    """Issue/PR numbers referenced as #123, owner/repo#123 or full URLs (same repo only)."""
    nums: list[int] = []
    for m in ISSUE_REF_RE.finditer(text or ""):
        if m.group("owner") and (m.group("owner").lower(), m.group("repo").lower()) != (
            owner.lower(),
            repo.lower(),
        ):
            continue
        nums.append(int(m.group("num")))
    for m in ISSUE_URL_RE.finditer(text or ""):
        if (m.group("owner").lower(), m.group("repo").lower()) == (owner.lower(), repo.lower()):
            nums.append(int(m.group("num")))
    return list(dict.fromkeys(nums))


def pr_number_from_message(message: str) -> int | None:
    first = (message or "").splitlines()[0] if message else ""
    m = MERGE_PR_RE.match(first) or SQUASH_PR_RE.search(first)
    return int(m.group(1)) if m else None


class GitHubClient:
    def __init__(self, conn: sqlite3.Connection, owner: str, repo: str, token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20) -> None:  # fmt: skip
        global _warned_no_token
        self.conn = conn
        self.owner = owner
        self.repo = repo
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.api_calls = 0
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "codeatlas",
                   "X-GitHub-Api-Version": "2022-11-28"}  # fmt: skip
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif not _warned_no_token:
            log.warning("GITHUB_TOKEN not set: GitHub API limited to 60 requests/hour")
            _warned_no_token = True
        self.http = httpx.AsyncClient(
            base_url=API, headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def _cache_get(self, url: str) -> tuple[int, Any] | None:
        row = self.conn.execute(
            "SELECT status, body FROM github_cache WHERE url = ?", (url,)
        ).fetchone()
        return (row["status"], json.loads(row["body"])) if row else None

    def _cache_put(self, url: str, status: int, body: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO github_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?)",
            (url, status, json.dumps(body), now_iso()),
        )

    async def get(self, path: str, params: dict | None = None) -> tuple[int, Any]:
        url = str(httpx.URL(API + path, params=params or {}))
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        if self.remaining == 0 and self.reset_at and self.reset_at > time.time():
            raise RateLimited(self.reset_at)
        for attempt in range(2):
            try:
                resp = await self.http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
            self.api_calls += 1
            self._track(resp)
            if resp.status_code in (403, 429) and (self.remaining == 0 or "retry-after" in resp.headers
                                                   or resp.status_code == 429):  # fmt: skip
                retry_after = float(resp.headers.get("retry-after", "0") or 0)
                if attempt == 0 and 0 < retry_after <= 10:
                    await asyncio.sleep(retry_after)
                    continue
                raise RateLimited(
                    self.reset_at or (time.time() + retry_after if retry_after else None)
                )
            break
        if resp.status_code in (200, 404, 422):
            body = resp.json() if resp.content else None
            self._cache_put(url, resp.status_code, body)
            return resp.status_code, body
        raise GitHubError(f"GitHub API returned {resp.status_code} for {path}")

    def _track(self, resp: httpx.Response) -> None:
        if "x-ratelimit-remaining" in resp.headers:
            self.remaining = int(resp.headers["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in resp.headers:
            self.reset_at = float(resp.headers["x-ratelimit-reset"])

    # -- endpoints -----------------------------------------------------------------------------
    async def prs_for_commit(self, sha: str) -> list[dict]:
        status, body = await self.get(f"/repos/{self.owner}/{self.repo}/commits/{sha}/pulls")
        return body if status == 200 and isinstance(body, list) else []

    async def pull(self, number: int) -> dict | None:
        status, body = await self.get(f"/repos/{self.owner}/{self.repo}/pulls/{number}")
        return body if status == 200 else None

    async def review_comments(self, number: int, limit: int = 10) -> list[dict]:
        status, body = await self.get(f"/repos/{self.owner}/{self.repo}/pulls/{number}/comments",
                                      {"per_page": limit})  # fmt: skip
        return body[:limit] if status == 200 and isinstance(body, list) else []

    async def issue(self, number: int) -> dict | None:
        status, body = await self.get(f"/repos/{self.owner}/{self.repo}/issues/{number}")
        return body if status == 200 else None
