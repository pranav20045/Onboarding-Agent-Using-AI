from __future__ import annotations

import pytest

from app.core.ingest.url import InvalidRepoURL, parse_github_url, validate_commit_sha


@pytest.mark.parametrize(
    ("url", "owner", "name"),
    [
        ("https://github.com/psf/requests", "psf", "requests"),
        ("https://github.com/psf/requests.git", "psf", "requests"),
        ("https://github.com/psf/requests/", "psf", "requests"),
        ("https://github.com/fastapi/fastapi", "fastapi", "fastapi"),
        ("https://github.com/a-b/x.y_z-1", "a-b", "x.y_z-1"),
    ],
)
def test_valid(url: str, owner: str, name: str) -> None:
    ref = parse_github_url(url)
    assert (ref.owner, ref.name) == (owner, name)
    assert ref.url == f"https://github.com/{owner}/{name}"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://github.com/psf/requests",
        "ssh://git@github.com/psf/requests",
        "git@github.com:psf/requests.git",
        "https://github.com.evil.com/psf/requests",
        "https://evil.com/github.com/psf/requests",
        "https://user:pass@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://127.0.0.1/psf/requests",
        "http://169.254.169.254/latest/meta-data",
        "https://github.com/psf",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf/requests?x=1",
        "https://github.com/psf/requests#frag",
        "https://github.com/psf/..",
        "https://github.com/../requests",
        "https://github.com/psf/%2e%2e",
        "https://github.com/-psf/requests",
        "https://github.com/psf/requests\n",
        " https://github.com/psf/requests",
        "https://github.com/psf/re quests",
        "https://github.com/psf\\requests",
        "https://GITHUB.com/psf/requests",
        "https://github.com/psf/" + "a" * 300,
        "/local/path",
        "C:\\repos\\x",
        "",
        "--upload-pack=touch /tmp/x",
    ],
)
def test_invalid(url: str) -> None:
    with pytest.raises(InvalidRepoURL):
        parse_github_url(url)


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidRepoURL):
        parse_github_url(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("sha", ["abc1234", "a" * 40, "0123456789abcdef0123456789abcdef01234567"])
def test_valid_sha(sha: str) -> None:
    assert validate_commit_sha(sha) == sha


@pytest.mark.parametrize("sha", ["", "abc", "ABCDEF1", "g" * 40, "a" * 41, "--all", "HEAD", "main"])
def test_invalid_sha(sha: str) -> None:
    with pytest.raises(ValueError):
        validate_commit_sha(sha)
