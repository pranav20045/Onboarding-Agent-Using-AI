"""Security checks from TEST_PROMPT.md section 4, run against the real implementations.

The payload corpus lives in `qa/fixtures/security_payloads.json` (written by the QA audit while
the features did not exist yet); these tests were wired to the code once it was built.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT

PAYLOADS = json.loads(
    (REPO_ROOT / "qa" / "fixtures" / "security_payloads.json").read_text(encoding="utf-8")
)


def _find(module_name: str, *names: str):  # type: ignore[no-untyped-def]
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


_VALIDATOR_NAMES = ("validate_repo_url", "parse_github_url", "validate_github_url")


def _validator():  # type: ignore[no-untyped-def]
    return _find("app.core.ingest", *_VALIDATOR_NAMES) or _find(
        "app.core.ingest.url", *_VALIDATOR_NAMES
    )


def _rejects(validator, url: str) -> bool:  # type: ignore[no-untyped-def]
    try:
        result = validator(url)
    except Exception:
        return True
    return result in (None, False)


@pytest.mark.parametrize("url", PAYLOADS["repo_url_must_reject"])
def test_repo_url_rejected(url: str) -> None:
    validator = _validator()
    if validator is None:
        pytest.skip("no repo URL validator in app.core.ingest yet (Phase 1)")
    assert _rejects(validator, url), url


@pytest.mark.parametrize("url", PAYLOADS["repo_url_must_accept"])
def test_repo_url_accepted(url: str) -> None:
    validator = _validator()
    if validator is None:
        pytest.skip("no repo URL validator in app.core.ingest yet (Phase 1)")
    assert not _rejects(validator, url), url


@pytest.fixture(scope="module")
def edge_ctx(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """The QA edge repo, indexed into a RepoContext (only walker-accepted files are readable)."""
    import importlib.util as iu

    from app.core.ingest.walk import walk_repo
    from app.core.models import RepoRef, sha256_hex
    from app.core.repo_context import RepoContext
    from app.db import store
    from app.db.connection import init_db

    spec = iu.spec_from_file_location(
        "qa_builder", REPO_ROOT / "qa" / "fixtures" / "build_qa_fixtures.py"
    )
    builder = iu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(builder)  # type: ignore[union-attr]
    tmp = tmp_path_factory.mktemp("qa_sec")
    repo = builder.build_edge_parse(tmp)
    (tmp / "outside_secret.py").write_text("SECRET_TOKEN = 'leaked'\n", encoding="utf-8")
    conn = init_db(tmp / "db.sqlite")
    repo_id = store.upsert_repo(conn, RepoRef("qa", "edge"))
    for f in walk_repo(repo).files:
        conn.execute(
            "INSERT INTO files (repo_id, path, content_hash, language, size_bytes) VALUES (?, ?, ?, ?, ?)",
            (repo_id, f.path, sha256_hex(f.text), f.language, f.size_bytes),
        )
    return RepoContext(repo_id, repo, conn)


@pytest.mark.parametrize("path", PAYLOADS["path_traversal_must_reject"])
def test_path_traversal_rejected(edge_ctx, path: str) -> None:  # type: ignore[no-untyped-def]
    """Wired to RepoContext (used by read_file, the verifier and GET /repos/{id}/file)."""
    from app.core.repo_context import PathError

    with pytest.raises((PathError, FileNotFoundError, ValueError)):
        edge_ctx.lines(path)


@pytest.mark.parametrize("pattern", PAYLOADS["grep_pattern_injection"])
def test_grep_injection_is_inert(edge_ctx, pattern: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Wired to the grep tool: payloads are regexes or rejected, never executed."""
    from app.core.agents import tools_grep
    from app.core.agents.toolbox import ToolInputError

    marker = Path("/tmp/pwned")
    existed = marker.exists()
    try:
        out = tools_grep.run_grep(edge_ctx, pattern, None, 20, "python")
        assert out.startswith("No matches") or " matches in " in out.splitlines()[0]
    except ToolInputError:
        pass
    assert marker.exists() == existed


@pytest.mark.parametrize("pattern", PAYLOADS["redos_patterns"])
def test_redos_patterns_bounded(edge_ctx, pattern: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Pathological regexes either finish or are killed at the timeout; never hang the server."""
    import time

    from app.core.agents import tools_grep

    monkeypatch.setattr(tools_grep, "GREP_TIMEOUT_S", 3)
    t0 = time.monotonic()
    try:
        tools_grep.run_grep(edge_ctx, pattern, None, 20, "python")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    assert time.monotonic() - t0 < 15


def test_webhook_pending() -> None:
    try:
        importlib.import_module("app.main")
    except ImportError:
        pytest.skip("no FastAPI app yet (Phase 6): webhook HMAC, rate limits, CORS, ADMIN_TOKEN")
