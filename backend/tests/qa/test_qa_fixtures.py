"""QA edge-case fixtures (TEST_PROMPT.md section 3).

1. Builds the QA fixture repos and verifies their shape (always runs).
2. Cross-checks the hand-written EXPECTED_UNITS ground truth against Python's `ast`
   (sources are parsed only, never imported or executed).
3. Parser / ingestion checks that skip until Phase 1 code exists in app.core.parse /
   app.core.ingest. When it lands, adapt `_find_callable` names if the API differs.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from app.config import REPO_ROOT

_BUILDER = REPO_ROOT / "qa" / "fixtures" / "build_qa_fixtures.py"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qa_build_qa_fixtures", _BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qa = _load_builder()


@pytest.fixture(scope="module")
def qa_repos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return qa.build_all(tmp_path_factory.mktemp("qa_fixtures"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


# --- 1. fixture shape ----------------------------------------------------------------------


def test_edge_repo_contents(qa_repos: dict[str, Path]) -> None:
    repo = qa_repos["edge_parse"]
    tree = _git(repo, "ls-tree", "-r", "HEAD").splitlines()
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in tree}
    for rel in qa.SYMLINKS:
        assert modes[rel] == "120000", f"{rel} must be committed as a symlink"
    assert "generated/ignored_build.py" in modes  # force-added despite .gitignore
    assert qa.DEEP_FILE in modes
    assert (repo / "pkg/huge_generated.py").stat().st_size > 1_000_000
    with pytest.raises(UnicodeDecodeError):
        (repo / "pkg/cp1252_no_cookie.py").read_bytes().decode("utf-8")
    assert b"\x00" in (repo / "pkg/binary_blob.py").read_bytes()
    for rel in ("pkg/syntax_error.py", "pkg/py2_only.py"):
        with pytest.raises(SyntaxError):
            ast.parse((repo / rel).read_text(encoding="utf-8"))


def test_empty_and_no_python_repos(qa_repos: dict[str, Path]) -> None:
    empty = qa_repos["empty_repo"]
    assert (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=empty, capture_output=True).returncode != 0
    )
    no_py = qa_repos["no_python"]
    assert not [p for p in _git(no_py, "ls-files").splitlines() if p.endswith(".py")]


# --- 2. ground truth self-check ------------------------------------------------------------


def _ast_units(source: str) -> list[tuple[str, str, int, int]]:
    units: list[tuple[str, str, int, int]] = []

    def visit(node: ast.AST, prefix: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                qname = f"{prefix}{child.name}"
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                else:
                    kind = "method" if in_class else "function"
                units.append((kind, qname, start, child.end_lineno or start))
                visit(child, qname + ".", isinstance(child, ast.ClassDef))
            else:
                visit(child, prefix, in_class)

    visit(ast.parse(source), "", False)
    return units


def test_expected_units_match_ast() -> None:
    assert len(qa.EXPECTED_UNITS) >= 20
    assert sorted(_ast_units(qa.UNITS_PY)) == sorted(qa.EXPECTED_UNITS)
    line = qa.UNITS_PY.splitlines()[qa.LAMBDA_LINE - 1]
    assert line.startswith("square = lambda")


# --- 3. pending: Phase 1 parser / ingestion ------------------------------------------------


def _find_callable(module_name: str, *names: str):  # type: ignore[no-untyped-def]
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    for name in names:
        if callable(getattr(module, name, None)):
            return getattr(module, name)
    return None


def test_parser_units_and_lines(qa_repos: dict[str, Path]) -> None:
    """Wired to the Phase 1 parser: every hand-counted unit, exact kinds and line ranges."""
    from app.core.parse.python_parser import parse_python_file

    pf = parse_python_file("pkg/units.py", qa.UNITS_PY)
    assert pf.status == "ok"
    got = sorted(
        (u.kind, u.qualified_name.removeprefix("pkg.units."), u.start_line, u.end_line)
        for u in pf.units
        if u.kind != "module"
    )
    assert got == sorted(qa.EXPECTED_UNITS)
    # Invalid Python 3 files fall back to text; the empty file still yields a module unit.
    edge = qa_repos["edge_parse"]
    for rel in ("pkg/py2_only.py", "pkg/syntax_error.py"):
        text = (edge / rel).read_text(encoding="utf-8", errors="replace")
        assert parse_python_file(rel, text).status == "syntax_error", rel
    assert [u.kind for u in parse_python_file("pkg/empty.py", "").units] == ["module"]


def test_ingest_skips_unsafe_and_vendored(qa_repos: dict[str, Path]) -> None:
    """Wired to the Phase 1 walker."""
    from app.core.ingest.walk import walk_repo

    edge = walk_repo(qa_repos["edge_parse"])
    paths = {f.path for f in edge.files}
    for rel in (
        "pkg/escape_relative.py",  # symlink escaping the repo
        "pkg/escape_absolute",  # symlink to /etc/passwd
        "venv/lib/python3.11/site-packages/vendored_lib/__init__.py",
        "node_modules/some_pkg/helper.py",
        "build/lib/pkg/units.py",
        "generated/ignored_build.py",  # tracked but matches .gitignore
        "pkg/huge_generated.py",  # > 1 MB
        "pkg/binary_blob.py",  # NUL bytes despite the .py suffix
    ):
        assert rel not in paths, rel
    assert "pkg/units.py" in paths and any(p.endswith("deep_module.py") for p in paths)
    assert not any("TOKEN" in f.text or "root:" in f.text for f in edge.files)
    cp = next(f for f in edge.files if f.path == "pkg/cp1252_no_cookie.py")
    assert cp.decode_replaced
    assert walk_repo(qa_repos["empty_repo"]).files == []
    assert {f.language for f in walk_repo(qa_repos["no_python"]).files} == {"text"}
