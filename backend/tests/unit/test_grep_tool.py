"""Security of the grep tool: injection, globs, ReDoS; both engines."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.agents import tools_grep
from app.core.agents.pygrep import glob_to_regex
from app.core.agents.toolbox import ToolInputError
from app.core.ingest.walk import walk_repo
from app.core.models import RepoRef, sha256_hex
from app.core.repo_context import RepoContext
from app.db import store
from app.db.connection import init_db


@pytest.fixture
def ctx(tmp_path: Path) -> RepoContext:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/mod.py").write_text(
        "def handler():\n    return 'x'\n# a;b $(echo hi) `id`\n", encoding="utf-8"
    )
    (root / "pkg/evil.py").write_text("a" * 40 + "!\n", encoding="utf-8")
    (root / "notes.md").write_text("handler docs\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    conn = init_db(tmp_path / "db.sqlite")
    repo_id = store.upsert_repo(conn, RepoRef("local", "g"))
    for f in walk_repo(root).files:
        conn.execute("INSERT INTO files (repo_id, path, content_hash, language, size_bytes) VALUES (?, ?, ?, ?, ?)",
                     (repo_id, f.path, sha256_hex(f.text), f.language, f.size_bytes))  # fmt: skip
    return RepoContext(repo_id, root, conn)


ENGINES = ["python"] + (["rg"] if tools_grep.rg_binary() else [])


@pytest.mark.parametrize("engine", ENGINES)
def test_basic_and_glob(ctx: RepoContext, engine: str) -> None:
    out = tools_grep.run_grep(ctx, r"def \w+", None, 50, engine)
    assert "pkg/mod.py:1:def handler():" in out
    out = tools_grep.run_grep(ctx, "handler", "*.md", 50, engine)
    assert "notes.md:1:handler docs" in out and "pkg/mod.py" not in out


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "pattern",
    ["--pre=/bin/sh", "-e", "$(echo hi)", "`id`", "a;b", "; rm -rf /", "| cat /etc/passwd"],
)
def test_injection_payloads_are_just_patterns(ctx: RepoContext, engine: str, pattern: str) -> None:
    try:
        out = tools_grep.run_grep(ctx, pattern, None, 50, engine)
    except ToolInputError:
        return  # rejected as an invalid regex: also fine
    assert "root:" not in out  # /etc/passwd was never executed or read
    assert out.startswith("No matches") or " matches in " in out.splitlines()[0]
    assert all(
        line.split(":")[0] in {"pkg/mod.py", "pkg/evil.py", "notes.md"}
        for line in out.splitlines()[1:]
    )


@pytest.mark.parametrize("glob", ["../*", "/etc/*", "C:/x", "a b", "**/../x", "$(id)"])
def test_bad_globs_rejected(ctx: RepoContext, glob: str) -> None:
    with pytest.raises(ToolInputError):
        tools_grep.run_grep(ctx, "x", glob, 10, "python")


@pytest.mark.parametrize("pattern", ["", "a\nb", "x" * 301, "(unclosed"])
def test_bad_patterns_rejected(ctx: RepoContext, pattern: str) -> None:
    with pytest.raises(ToolInputError):
        tools_grep.run_grep(ctx, pattern, None, 10, "python")


def test_redos_is_killed(ctx: RepoContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catastrophic backtracking in Python `re` runs in a child that is killed at the timeout."""
    monkeypatch.setattr(tools_grep, "GREP_TIMEOUT_S", 2)
    with pytest.raises(RuntimeError, match="timed out"):
        tools_grep.run_grep(ctx, r"^(a+)+$", None, 10, "python")


def test_results_limited_to_indexed_files(ctx: RepoContext) -> None:
    (ctx.checkout / "pkg/unindexed.py").write_text("handler\n", encoding="utf-8")
    out = tools_grep.run_grep(ctx, "handler", None, 50, "python")
    assert "unindexed" not in out


@pytest.mark.parametrize(
    ("glob", "path", "ok"),
    [("**/*.py", "a/b/c.py", True), ("*.py", "a/b/c.py", True), ("src/*.py", "src/a/b.py", False),
     ("src/**/*.py", "src/a/b.py", True), ("*.{md,rst}", "docs/x.rst", True), ("tests/*", "x/tests/a", False)],
)  # fmt: skip
def test_glob_semantics(glob: str, path: str, ok: bool) -> None:
    assert bool(glob_to_regex(glob).match(path)) is ok
