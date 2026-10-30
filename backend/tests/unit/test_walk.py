from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.core.ingest.walk import TooManyFiles, WalkConfig, classify, walk_repo


def test_fixture_walk(fixture_repo: Path) -> None:
    result = walk_repo(fixture_repo)
    paths = {f.path for f in result.files}
    assert "shop/services.py" in paths
    assert "README.md" in paths and "pyproject.toml" in paths
    assert "shop/discounts.py" not in paths  # deleted in history
    # PEP 263 cookie honoured: decoded correctly, no replacement characters.
    latin = next(f for f in result.files if f.path == "shop/locale_fr.py")
    assert not latin.decode_replaced and "café" in latin.text
    # Cookie-less cp1252 text: decoded with replacement and a warning.
    notes = next(f for f in result.files if f.path == "docs/release_notes.txt")
    assert notes.decode_replaced and "�" in notes.text
    assert [w for w in result.warnings if "release_notes.txt" in w]
    assert not [w for w in result.warnings if "locale_fr.py" in w]
    assert all("\\" not in p for p in paths)  # POSIX separators everywhere


def _make_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_ignore_rules(tmp_path: Path) -> None:
    _make_tree(
        tmp_path,
        {
            "pkg/mod.py": b"x = 1\n",
            "node_modules/lib/index.py": b"x = 1\n",
            ".venv/lib/site.py": b"x = 1\n",
            "build/out.py": b"x = 1\n",
            "pkg/gen_pb2.py": b"x = 1\n",
            "poetry.lock": b"[x]\n",
            "pkg/big.py": b"#" * 2000 + b"\n",
            "pkg/blob.py": b"\x00\x01binary",
            "pkg/image.png": b"\x89PNG",
            "docs/guide.md": b"# Guide\n",
        },
    )
    result = walk_repo(tmp_path, WalkConfig(max_file_size_bytes=1000))
    assert {f.path for f in result.files} == {"pkg/mod.py", "docs/guide.md"}
    assert "binary" in result.skipped["pkg/blob.py"]
    assert "larger" in result.skipped["pkg/big.py"]
    assert "unsupported" in result.skipped["pkg/image.png"]
    assert "ignored" in result.skipped["node_modules/lib/index.py"]


def test_gitignore_respected(tmp_path: Path) -> None:
    _make_tree(tmp_path, {"a.py": b"x=1\n", "secret/b.py": b"x=1\n", ".gitignore": b"secret/\n"})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = walk_repo(tmp_path)
    paths = {f.path for f in result.files}
    assert "a.py" in paths and "secret/b.py" not in paths


def test_symlink_outside_repo_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    try:
        os.symlink(outside / "secret.py", repo / "link.py")
        os.symlink(outside, repo / "linkdir", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this machine")
    result = walk_repo(repo)
    paths = {f.path for f in result.files}
    assert paths == {"ok.py"}
    assert not any("TOKEN" in f.text for f in result.files)


def test_too_many_python_files(tmp_path: Path) -> None:
    _make_tree(tmp_path, {f"m{i}.py": b"x = 1\n" for i in range(5)})
    with pytest.raises(TooManyFiles):
        walk_repo(tmp_path, WalkConfig(max_python_files=3))


def test_empty_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert walk_repo(tmp_path).files == []


def test_bom_stripped(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
    (f,) = walk_repo(tmp_path).files
    assert f.text == "x = 1\n" and not f.decode_replaced


@pytest.mark.parametrize(
    ("path", "lang"),
    [("a/b.py", "python"), ("x.pyi", "python"), ("README", "text"), ("README.md", "text"),
     ("setup.cfg", "text"), ("Makefile", "text"), ("a.png", None), ("a.js", None)],
)  # fmt: skip
def test_classify(path: str, lang: str | None) -> None:
    assert classify(path) == lang


def test_ignore_list_is_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings
    from app.core.pipeline import walk_config

    _make_tree(
        tmp_path,
        {
            "app/main.py": b"x = 1\n",
            "vendor/lib/v.py": b"x = 1\n",  # ignored by default
            "docs_src/tut/t.py": b"x = 1\n",  # indexed by default
            "app/schema_generated.py": b"x = 1\n",
        },
    )

    def indexed(**env: str) -> set[str]:
        for k in ("EXTRA_IGNORE_DIRS", "EXTRA_IGNORE_GLOBS", "UNIGNORE_DIRS"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        cfg = walk_config(Settings(_env_file=None, data_dir=tmp_path / "d"))  # type: ignore[call-arg]
        return {f.path for f in walk_repo(tmp_path, cfg).files}

    base = indexed()
    assert base == {"app/main.py", "docs_src/tut/t.py", "app/schema_generated.py"}
    # comma-separated and JSON-list forms both work
    assert "docs_src/tut/t.py" not in indexed(EXTRA_IGNORE_DIRS="docs_src,other")
    assert "docs_src/tut/t.py" not in indexed(EXTRA_IGNORE_DIRS='["docs_src"]')
    assert "app/schema_generated.py" not in indexed(EXTRA_IGNORE_GLOBS="*_generated.py")
    assert "vendor/lib/v.py" in indexed(UNIGNORE_DIRS="vendor")
    # built-in defaults still apply on top of the extras
    assert "app/main.py" in indexed(EXTRA_IGNORE_DIRS="docs_src")
