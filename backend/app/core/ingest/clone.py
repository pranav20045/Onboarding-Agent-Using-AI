"""Clone a validated GitHub repo into a per-repo working directory, enforcing size limits."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from app.core.gitutil import HTTPS_ONLY, GitError, run_git
from app.core.ingest.url import validate_commit_sha
from app.core.models import RepoRef

log = logging.getLogger(__name__)


class RepoTooLarge(RuntimeError):
    pass


class CloneError(RuntimeError):
    pass


def github_reported_size_mb(
    ref: RepoRef, token: str | None = None, timeout: float = 10
) -> float | None:
    """Best-effort pre-clone size check via the GitHub API (`size` is in KB). None if unknown."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{ref.owner}/{ref.name}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "codeatlas"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("Pre-clone size check for %s failed: %s", ref.slug, exc)
        return None
    size_kb = data.get("size")
    return size_kb / 1024 if isinstance(size_kb, int | float) else None


def dir_size_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp):
                with contextlib.suppress(OSError):
                    total += os.path.getsize(fp)
    return total / (1024 * 1024)


def clone_repo(
    ref: RepoRef,
    dest: Path,
    *,
    max_size_mb: int,
    commit_sha: str | None = None,
    github_token: str | None = None,
    clone_url: str | None = None,
    timeout: float = 900,
) -> str:
    """Clone (or refresh) `ref` into `dest`, optionally detaching at `commit_sha`.

    Returns the checked-out HEAD sha. `clone_url` overrides the source (tests use a local path);
    the public API never passes it, so only validated GitHub URLs reach git there.
    """
    if commit_sha:
        validate_commit_sha(commit_sha)
    if clone_url is None:
        reported = github_reported_size_mb(ref, github_token)
        if reported is not None and reported > max_size_mb:
            raise RepoTooLarge(
                f"{ref.slug} is {reported:.0f} MB according to GitHub (limit {max_size_mb} MB)"
            )
    source = clone_url or f"{ref.url}.git"
    protocols = None if clone_url else HTTPS_ONLY

    if (dest / ".git").is_dir():
        run_git(["fetch", "--quiet", "--prune", "origin"], cwd=dest, timeout=timeout,
                extra_config=protocols)  # fmt: skip
        target = commit_sha or "origin/HEAD"
    else:
        if dest.exists():
            shutil.rmtree(dest, onerror=_force_remove)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Full history is required for blame. `--` stops option injection via the source.
            run_git(["clone", "--quiet", "--no-recurse-submodules", "--", source, str(dest)],
                    timeout=timeout, extra_config=protocols)  # fmt: skip
        except GitError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise CloneError(f"Could not clone {ref.slug}: {exc}") from exc
        target = commit_sha or "HEAD"

    try:
        run_git(["checkout", "--quiet", "--detach", "--force", target], cwd=dest)
    except GitError as exc:
        raise CloneError(f"Could not check out {target}: {exc}") from exc

    size = dir_size_mb(dest)
    if size > max_size_mb:
        shutil.rmtree(dest, onerror=_force_remove)
        raise RepoTooLarge(f"{ref.slug} is {size:.0f} MB on disk (limit {max_size_mb} MB)")
    return head_sha(dest)


def head_sha(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=repo).strip()


def _force_remove(func, path, _exc_info) -> None:
    os.chmod(path, 0o700)
    func(path)
