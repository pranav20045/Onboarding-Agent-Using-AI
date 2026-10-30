from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from app.core.ingest.walk import walk_repo
from app.core.models import SourceFile, sha256_hex, split_lines
from app.core.parse.chunking import ChunkConfig, chunks_for_file, doc_chunks, fixed_chunks
from app.core.parse.python_parser import parse_python_file


def _src(path: str, text: str, language: str = "python") -> SourceFile:
    return SourceFile(path, language, len(text), sha256_hex(text), text)  # type: ignore[arg-type]


def test_every_chunk_has_valid_lines(fixture_repo: Path) -> None:
    for src in walk_repo(fixture_repo).files:
        parsed = parse_python_file(src.path, src.text) if src.language == "python" else None
        lines = split_lines(src.text)
        for strategy in ("ast", "fixed"):
            for c in chunks_for_file(src, parsed, strategy):
                assert 1 <= c.start_line <= c.end_line <= len(lines), (src.path, c)
                assert c.text.startswith(f"# {src.path}:")


def test_ast_chunks_for_services(fixture_repo: Path) -> None:
    src = next(f for f in walk_repo(fixture_repo).files if f.path == "shop/services.py")
    chunks = chunks_for_file(src, parse_python_file(src.path, src.text), "ast")
    by_name = {(c.qualified_name, c.unit_kind) for c in chunks}
    assert ("shop.services.OrderService.create_order", "method") in by_name
    assert ("shop.services.OrderService", "class") in by_name
    summary = next(c for c in chunks if c.unit_kind == "class")
    assert (
        "def create_order(self, name: str, products: list[Product]) -> Order: ..." in summary.text
    )
    assert "self.validate_order(order)" not in summary.text  # method bodies collapsed
    module_level = [c for c in chunks if c.unit_kind == "module"]
    assert module_level and "from shop.models import Order, Product" in module_level[0].text


def test_nested_functions_stay_in_parent(fixture_repo: Path) -> None:
    src = next(f for f in walk_repo(fixture_repo).files if f.path == "shop/utils.py")
    chunks = chunks_for_file(src, parse_python_file(src.path, src.text), "ast")
    names = [c.qualified_name for c in chunks if c.unit_kind == "function"]
    assert names == ["shop.utils.retry"]
    assert "def wrapper" in chunks[-1].text


def test_large_unit_is_split_with_header() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(400))
    text = f"def big():\n    '''Doc.'''\n{body}\n    return 1\n"
    src = _src("m.py", text)
    chunks = chunks_for_file(
        src, parse_python_file("m.py", text), "ast", ChunkConfig(max_chars=1000)
    )
    parts = [c for c in chunks if c.qualified_name == "m.big"]
    assert len(parts) > 3
    assert all("part " in c.text and "def big()" in c.text for c in parts)
    assert parts[0].start_line == 1 and parts[-1].end_line == len(split_lines(text))
    for a, b in pairwise(parts):
        assert b.start_line <= a.end_line + 1  # contiguous or overlapping
    assert len({c.id for c in parts}) == len(parts)


def test_syntax_error_file_gets_text_chunks(fixture_repo: Path) -> None:
    src = next(f for f in walk_repo(fixture_repo).files if f.path == "legacy/export_report.py")
    chunks = chunks_for_file(src, parse_python_file(src.path, src.text), "ast")
    assert chunks and all(c.kind == "text_fallback" for c in chunks)


def test_markdown_split_by_headings() -> None:
    text = "# Title\n\nintro\n\n## A\n\n" + ("para a\n" * 300) + "\n## B\n\nbody b\n"
    chunks = doc_chunks(_src("README.md", text, "text"), ChunkConfig(max_chars=900))
    assert any(c.text.split("\n", 1)[1].startswith("## B") for c in chunks)
    assert all(c.kind == "doc" for c in chunks)


def test_fixed_chunks_cover_file() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 501)) + "\n"
    chunks = fixed_chunks(
        _src("a.py", text), ChunkConfig(fixed_size_chars=500, fixed_overlap_chars=120)
    )
    covered = set()
    for c in chunks:
        covered.update(range(c.start_line, c.end_line + 1))
    assert covered == set(range(1, 501))
    assert chunks[1].start_line < chunks[0].end_line + 1  # overlap


def test_chunk_ids_are_deterministic(fixture_repo: Path) -> None:
    src = next(f for f in walk_repo(fixture_repo).files if f.path == "shop/models.py")
    a = chunks_for_file(src, parse_python_file(src.path, src.text), "ast")
    b = chunks_for_file(src, parse_python_file(src.path, src.text), "ast")
    assert [c.id for c in a] == [c.id for c in b]
