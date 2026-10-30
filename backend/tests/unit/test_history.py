"""Git mining, blame, and GitHub linking with a mocked transport (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.history.git_history import blame, git_show, mine_commits, resolve_sha
from app.core.history.github import GitHubClient, issue_refs, pr_number_from_message
from app.core.history.linking import commit_record, link_commit
from app.core.history.stage import history_stage
from app.core.history.tools import format_commit
from app.core.models import RepoRef
from app.db import store
from app.db.connection import init_db
from tests.fakes import make_services


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "h.db")
    store.upsert_repo(c, RepoRef("acme", "shop"))
    return c


def test_mine_and_blame(fixture_repo: Path, conn) -> None:
    assert mine_commits(conn, "acme__shop", fixture_repo) == 7
    rows = conn.execute("SELECT message, files_json FROM commits ORDER BY authored_at").fetchall()
    assert rows[0]["message"].startswith("Initial project skeleton")
    assert "shop/payments.py" in json.loads(rows[3]["files_json"])
    lines = blame(fixture_repo, "shop/payments.py", 6, 7)
    assert lines[0].summary.startswith(
        "Retry payment gateway calls 3 times"
    )  # line 6: the decorator
    assert lines[1].summary.startswith("Add order service")  # line 7: unchanged since creation
    rec = commit_record(conn, "acme__shop", lines[0].sha[:8])
    assert "Fixes #42" in rec["message"] and rec["files"] == ["shop/payments.py"]
    assert "times=3, backoff=0.5" in git_show(fixture_repo, lines[0].sha[:10])


def test_blame_and_sha_validation(fixture_repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_sha(fixture_repo, "--output=/tmp/x")
    with pytest.raises(ValueError):
        blame(fixture_repo, "../outside.py", 1, 2)
    with pytest.raises(ValueError):
        blame(fixture_repo, "shop/api.py", 5, 2)


def test_issue_ref_parsing() -> None:
    text = "Fixes #42, see acme/shop#7 and other/repo#9 and https://github.com/acme/shop/issues/11"
    assert issue_refs(text, "acme", "shop") == [42, 7, 11]
    assert pr_number_from_message("Retry fix (#123)\n\nbody") == 123
    assert pr_number_from_message("Merge pull request #77 from x/y") == 77
    assert pr_number_from_message("Fixes #42") is None


def _mock_github(calls: list[str], rate_limited: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        headers = {
            "x-ratelimit-remaining": "0" if rate_limited else "4999",
            "x-ratelimit-reset": "9999999999",
        }
        if rate_limited:
            return httpx.Response(403, headers=headers, json={"message": "API rate limit exceeded"})
        path = request.url.path
        if path.endswith("/pulls") and "/commits/" in path:
            pr = {
                "number": 5,
                "title": "Retry PayCo calls",
                "body": "Fixes #42. Nightly 503s.",
                "html_url": "https://github.com/acme/shop/pull/5",
                "state": "closed",
                "merged_at": "2024-01-04",
            }
            return httpx.Response(200, headers=headers, json=[pr])
        if path.endswith("/pulls/5/comments"):
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "user": {"login": "rev"},
                        "path": "shop/payments.py",
                        "body": "Why not 5 retries?",
                    }
                ],
            )
        if path.endswith("/issues/42"):
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "number": 42,
                    "title": "Checkout fails at 2am",
                    "body": "503 from PayCo",
                    "state": "closed",
                    "html_url": "https://github.com/acme/shop/issues/42",
                },
            )
        return httpx.Response(404, headers=headers, json={"message": "Not Found"})  # fmt: skip

    return httpx.MockTransport(handler)


async def test_link_commit_and_cache(fixture_repo: Path, conn) -> None:
    mine_commits(conn, "acme__shop", fixture_repo)
    sha = blame(fixture_repo, "shop/payments.py", 6, 6)[0].sha
    calls: list[str] = []
    gh = GitHubClient(conn, "acme", "shop", token=None, transport=_mock_github(calls))
    msg = commit_record(conn, "acme__shop", sha)["message"]
    assert await link_commit(conn, gh, "acme__shop", sha, msg) == "linked"
    rec = commit_record(conn, "acme__shop", sha)
    pr = rec["pull_requests"][0]
    assert pr["number"] == 5 and pr["review_comments"][0]["body"] == "Why not 5 retries?"
    assert pr["issues"][0]["title"] == "Checkout fails at 2am"
    text = format_commit(rec)
    assert "Pull request #5" in text and "Linked issue #42" in text
    n = len(calls)
    gh2 = GitHubClient(conn, "acme", "shop", transport=_mock_github(calls))
    await gh2.prs_for_commit(sha)
    assert len(calls) == n  # served from the SQLite cache


async def test_rate_limit_degrades_gracefully(fixture_repo: Path, conn) -> None:
    mine_commits(conn, "acme__shop", fixture_repo)
    sha = blame(fixture_repo, "shop/payments.py", 6, 6)[0].sha
    gh = GitHubClient(conn, "acme", "shop", transport=_mock_github([], rate_limited=True))
    status = await link_commit(conn, gh, "acme__shop", sha, "Retry payment gateway calls")
    assert status == "rate_limited"
    text = format_commit(commit_record(conn, "acme__shop", sha))
    assert "rate limit" in text and "commit message alone" in text


async def test_history_stage_offline_and_online(fixture_repo: Path, tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    local_id = store.upsert_repo(svc.conn, RepoRef("local", "shopkit"))
    stats = await history_stage(svc, local_id, fixture_repo, lambda d, t: None)
    assert stats["commits"] == 7 and "no GitHub remote" in stats["note"]

    online_id = store.upsert_repo(svc.conn, RepoRef("acme", "shop"))
    calls: list[str] = []
    gh = GitHubClient(svc.conn, "acme", "shop", transport=_mock_github(calls))
    stats = await history_stage(svc, online_id, fixture_repo, lambda d, t: None, client=gh)
    assert stats["prelink_target"] == 7 and stats["linked"] == 7  # mock returns PR #5 for every sha
    assert stats["github_api_calls"] == len(calls)


def _seed_commits(conn, repo_id: str, hot_touchers: set[int], total: int = 12) -> None:
    for i in range(total):
        files = ["hot.py"] if i in hot_touchers else []
        files.append(f"f{i}.py")
        conn.execute(
            "INSERT INTO commits (repo_id, sha, author, authored_at, message, is_merge, files_json) "
            "VALUES (?, ?, 'a', ?, ?, 0, ?)",
            (repo_id, f"c{i:02d}", f"2024-01-{i + 1:02d}T00:00:00", f"msg {i}", json.dumps(files)),
        )


def test_prelink_mixes_most_changed_and_most_recent(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.history import linking

    monkeypatch.setattr(linking, "HOT_FILES", 1)
    _seed_commits(conn, "acme__shop", hot_touchers={1, 2, 3})  # old commits that churn hot.py
    picked = [sha for sha, _ in linking.prelink_candidates(conn, "acme__shop", 4)]
    # half from the commits touching the most-changed file (newest first), half most recent,
    # interleaved so a rate limit mid-run still covers both groups
    assert picked == ["c03", "c11", "c02", "c10"]
    assert linking.prelink_candidates(conn, "acme__shop", 0) == []
    assert len(linking.prelink_candidates(conn, "acme__shop", 100)) == 12  # never more than exist
    # already-linked commits are skipped; rate-limited ones are retried
    conn.execute("INSERT INTO commit_link_status VALUES ('acme__shop', 'c11', 'linked', NULL, 'x')")
    conn.execute(
        "INSERT INTO commit_link_status VALUES ('acme__shop', 'c03', 'rate_limited', NULL, 'x')"
    )
    picked = [sha for sha, _ in linking.prelink_candidates(conn, "acme__shop", 4)]
    assert "c11" not in picked and picked[0] == "c03" and picked[1] == "c10"


def test_prelink_without_hot_files_falls_back_to_recent(conn) -> None:
    from app.core.history import linking

    _seed_commits(conn, "acme__shop", hot_touchers=set(), total=6)
    picked = [sha for sha, _ in linking.prelink_candidates(conn, "acme__shop", 3)]
    assert picked == ["c05", "c04", "c03"]
