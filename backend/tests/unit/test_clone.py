from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.ingest.clone import RepoTooLarge, clone_repo, head_sha
from app.core.models import RepoRef

REF = RepoRef("local", "shopkit")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_clone_and_pin(fixture_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "c"
    sha = clone_repo(REF, dest, max_size_mb=50, clone_url=str(fixture_repo))
    assert sha == _git(fixture_repo, "rev-parse", "HEAD").strip()
    assert len(_git(dest, "log", "--format=%H").split()) == 7  # full history kept

    first = _git(fixture_repo, "rev-list", "--max-parents=0", "HEAD").strip()
    pinned = clone_repo(REF, dest, max_size_mb=50, commit_sha=first, clone_url=str(fixture_repo))
    assert pinned == first == head_sha(dest)
    assert not (dest / "shop/services.py").exists()  # added in a later commit


def test_size_limit_enforced(fixture_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "c"
    with pytest.raises(RepoTooLarge):
        clone_repo(REF, dest, max_size_mb=0, clone_url=str(fixture_repo))
    assert not dest.exists()  # cleaned up


def test_bad_sha_rejected_before_git(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        clone_repo(REF, tmp_path / "c", max_size_mb=50, commit_sha="--upload-pack=evil",
                   clone_url="unused")  # fmt: skip


def test_https_only_protocols(tmp_path: Path) -> None:
    """A GitHub clone must never be able to use file:// even if a URL slipped through."""
    from app.core.gitutil import HTTPS_ONLY, GitError, run_git

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    with pytest.raises(GitError):
        run_git(["clone", "--", src.as_uri(), str(tmp_path / "dst")], extra_config=HTTPS_ONLY)
