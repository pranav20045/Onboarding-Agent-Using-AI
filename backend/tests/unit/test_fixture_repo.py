"""The fixture repo is the ground truth for most later tests, so check it is built as specified.

Note: these tests only *parse* fixture sources with `ast` (never import or execute them).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from tests.fixtures.build_fixture_repo import (
    DESIGN_COMMIT_SUBJECT,
    PROMPT_INJECTION,
    build_fixture_repo,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_history_shape(fixture_repo: Path) -> None:
    subjects = git(fixture_repo, "log", "--format=%s").splitlines()
    assert len(subjects) == 7
    assert DESIGN_COMMIT_SUBJECT in subjects
    body = git(fixture_repo, "log", "--format=%B", "-n1", "--grep", "exponential backoff")
    assert "Fixes #42" in body
    assert "double-charging" in body


def test_rename_and_delete_in_history(fixture_repo: Path) -> None:
    status = git(fixture_repo, "log", "-M", "--name-status", "--format=")
    assert any(
        ln.startswith("R") and ln.endswith("shop/helpers.py\tshop/text_utils.py")
        for ln in status.splitlines()
    )
    assert "D\tshop/discounts.py" in status
    assert not (fixture_repo / "shop/discounts.py").exists()
    assert not (fixture_repo / "shop/helpers.py").exists()
    assert (fixture_repo / "shop/text_utils.py").exists()


def test_parse_edge_cases_present(fixture_repo: Path) -> None:
    for rel in ("legacy/export_report.py", "shop/wip_refactor.py"):
        src = (fixture_repo / rel).read_text(encoding="utf-8")
        try:
            ast.parse(src)
        except SyntaxError:
            pass
        else:
            raise AssertionError(f"{rel} should not parse")

    raw = (fixture_repo / "shop/locale_fr.py").read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("locale_fr.py should not be valid UTF-8")

    api = ast.parse((fixture_repo / "shop/api.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(api))
    decorated = [n for n in ast.walk(api) if isinstance(n, ast.FunctionDef) and n.decorator_list]
    assert decorated

    utils = ast.parse((fixture_repo / "shop/utils.py").read_text(encoding="utf-8"))
    retry = next(n for n in utils.body if isinstance(n, ast.FunctionDef) and n.name == "retry")
    nested = [n.name for n in ast.walk(retry) if isinstance(n, ast.FunctionDef)]
    assert {"decorator", "wrapper"} <= set(nested)

    models = ast.parse((fixture_repo / "shop/models.py").read_text(encoding="utf-8"))
    bases = {
        n.name: [b.id for b in n.bases if isinstance(b, ast.Name)]
        for n in models.body
        if isinstance(n, ast.ClassDef)
    }
    assert bases["Product"] == ["Entity"]
    assert bases["DigitalProduct"] == ["Product"]

    services = (fixture_repo / "shop/services.py").read_text(encoding="utf-8")
    assert "self.validate_order(order)" in services
    payments = (fixture_repo / "shop/payments.py").read_text(encoding="utf-8")
    assert "import retry as with_retry" in payments
    assert "times=3" in payments


def test_readme_contains_prompt_injection(fixture_repo: Path) -> None:
    assert PROMPT_INJECTION in (fixture_repo / "README.md").read_text(encoding="utf-8")


def test_build_is_deterministic(tmp_path: Path, fixture_repo: Path) -> None:
    other = build_fixture_repo(tmp_path / "again")
    assert git(other, "rev-parse", "HEAD") == git(fixture_repo, "rev-parse", "HEAD")
    assert git(other, "status", "--porcelain") == ""


def test_rebuild_replaces_existing(tmp_path: Path) -> None:
    dest = build_fixture_repo(tmp_path / "r")
    (dest / "stray.txt").write_text("x", encoding="utf-8")
    build_fixture_repo(dest)
    assert not (dest / "stray.txt").exists()
