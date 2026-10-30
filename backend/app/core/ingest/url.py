"""Strict GitHub URL validation.

Only `https://github.com/<owner>/<repo>` (optionally `.git` and/or a trailing slash) is accepted.
Everything else (other schemes, hosts, credentials, ports, IPs, paths, queries) is rejected,
because the URL is handed to `git clone` and must never reach local files or internal hosts.
"""

from __future__ import annotations

import re

from app.core.models import RepoRef

MAX_URL_LENGTH = 200

# GitHub rules: owner 1-39 chars of alnum/hyphen, not starting with a hyphen.
# Repo names: alnum, '.', '_', '-' (max 100).
_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)


class InvalidRepoURL(ValueError):
    pass


def parse_github_url(url: str) -> RepoRef:
    if not isinstance(url, str):
        raise InvalidRepoURL("URL must be a string")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidRepoURL(f"URL longer than {MAX_URL_LENGTH} characters")
    if url != url.strip() or any(ord(c) < 0x21 or ord(c) > 0x7E for c in url):
        raise InvalidRepoURL("URL contains whitespace, control or non-ASCII characters")
    if "%" in url or "\\" in url:
        raise InvalidRepoURL("URL must not contain percent-encoding or backslashes")
    match = _URL_RE.match(url)
    if not match:
        raise InvalidRepoURL("Only https://github.com/<owner>/<repo> URLs are accepted")
    repo = match.group("repo")
    if repo in {".", ".."} or repo.startswith("."):
        raise InvalidRepoURL("Invalid repository name")
    return RepoRef(owner=match.group("owner"), name=repo)


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def validate_commit_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise ValueError("commit_sha must be 7-40 lowercase hex characters")
    return sha
