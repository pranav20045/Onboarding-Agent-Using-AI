from __future__ import annotations

from pathlib import Path

import pytest

from app.core.agents.verifier import extract_identifiers, parse_citations, verify
from app.core.indexer import create_job, run_index_job
from app.core.models import RepoRef
from app.core.repo_context import PathError, safe_path
from app.db import store
from tests.fakes import make_services


@pytest.fixture
async def ctx(fixture_repo: Path, tmp_path: Path):
    svc = make_services(tmp_path)
    ref = RepoRef("local", "shopkit")
    repo_id = store.upsert_repo(svc.conn, ref)
    await run_index_job(
        svc, repo_id, create_job(svc, repo_id), ref=ref, clone_url=str(fixture_repo)
    )
    return svc.repo_context(repo_id)


def test_parse_citations() -> None:
    cs = parse_citations("See [shop/a.py:10-20] and [b/c.py:5], not [x] or [a.py:x-y].")
    assert [(c.path, c.start, c.end) for c in cs] == [("shop/a.py", 10, 20), ("b/c.py", 5, 5)]


def test_extract_identifiers() -> None:
    text = "Uses `OrderService.create_order()` and `slugify`, `self`, `len`, `x = 1`, `a/b.py`."
    assert extract_identifiers(text) == ["OrderService.create_order", "slugify"]


async def test_valid_answer_passes(ctx) -> None:
    r = verify("`OrderService.validate_order` raises when empty [shop/services.py:20-22].", ctx)
    assert r.ok and len(r.citations) == 1


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("x [shop/missing.py:1-2]", "file not found"),
        ("x [shop/services.py:20-999]", "exceeds file length"),
        ("x [shop/services.py:30-20]", "invalid line range"),
        ("x [../etc/passwd:1-2]", "file not found"),
        ("x [.git/config:1-2]", "file not found"),
        ("x [shop/discounts.py:1-2]", "file not found"),  # deleted in history: not indexed
    ],
)
async def test_invalid_citations(ctx, answer: str, reason: str) -> None:
    r = verify(answer, ctx)
    assert not r.ok and reason in r.invalid_citations[0][1]


async def test_hallucinated_symbols(ctx) -> None:
    r = verify(
        "Call `OrderService.refund_order` or `PaymentRetryPolicy` [shop/services.py:20-22].", ctx
    )
    assert r.hallucinated_symbols == ["OrderService.refund_order", "PaymentRetryPolicy"]
    ok = verify("It uses `functools.wraps` and `time.sleep`.", ctx)  # stdlib names used in the repo
    assert ok.ok


@pytest.mark.parametrize(
    "bad",
    [
        "../x",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "a/../../x",
        "a\x00b",
        ".git/config",
        "~/x",
        "",
        "  ",
    ],
)
def test_safe_path_rejects(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathError):
        safe_path(tmp_path, bad)


def test_safe_path_symlink_escape(tmp_path: Path) -> None:
    import os

    root = tmp_path / "repo"
    root.mkdir()
    try:
        os.symlink(tmp_path, root / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted")
    with pytest.raises(PathError):
        safe_path(root, "escape/secret.txt")
