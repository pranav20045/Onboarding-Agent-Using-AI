from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import split_lines
from app.core.parse.python_parser import module_name_for, parse_python_file


def _parse(fixture_repo: Path, rel: str):
    return parse_python_file(rel, (fixture_repo / rel).read_text(encoding="utf-8"))


def _line_of(fixture_repo: Path, rel: str, needle: str) -> int:
    lines = (fixture_repo / rel).read_text(encoding="utf-8").split("\n")
    return next(i for i, ln in enumerate(lines, start=1) if needle in ln)


def test_qualified_names_and_kinds(fixture_repo: Path) -> None:
    units = {u.qualified_name: u for u in _parse(fixture_repo, "shop/utils.py").units}
    assert units["shop.utils"].kind == "module"
    assert units["shop.utils.retry"].kind == "function"
    assert units["shop.utils.retry.decorator"].nested
    assert units["shop.utils.retry.decorator.wrapper"].parent == "shop.utils.retry.decorator"
    assert units["shop.utils.retry.decorator.wrapper"].decorators == ["functools.wraps(func)"]

    models = {u.qualified_name: u for u in _parse(fixture_repo, "shop/models.py").units}
    assert models["shop.models.Product"].kind == "class"
    assert models["shop.models.Product"].bases == ["Entity"]
    assert models["shop.models.Product.describe"].kind == "method"
    assert models["shop.models.Order"].decorators == ["dataclass"]
    assert models["shop.models.Entity"].docstring == "Base class for everything with an id."


def test_exact_line_ranges(fixture_repo: Path) -> None:
    """Spot-check unit boundaries against the real file text."""
    rel = "shop/api.py"
    pf = _parse(fixture_repo, rel)
    units = {u.qualified_name: u for u in pf.units}
    route = units["shop.api.create_order_route"]
    # Decorated definitions start at the decorator line.
    assert route.start_line == _line_of(fixture_repo, rel, '@router.post("/orders")')
    assert route.end_line == _line_of(fixture_repo, rel, 'return {"order_id"')
    inv = units["shop.api.fetch_inventory"]
    assert inv.is_async and inv.signature == "async def fetch_inventory() -> dict"
    assert inv.end_line == _line_of(fixture_repo, rel, 'return {"in_stock": 42}')
    lines = split_lines((fixture_repo / rel).read_text(encoding="utf-8"))
    for u in pf.units:
        assert u.code == "\n".join(lines[u.start_line - 1 : u.end_line])
    router = units["shop.api.Router"]
    assert router.start_line == _line_of(fixture_repo, rel, "class Router:")


def test_imports_and_aliases(fixture_repo: Path) -> None:
    pf = _parse(fixture_repo, "shop/payments.py")
    assert [(b.alias, b.target) for b in pf.imports] == [("with_retry", "shop.utils.retry")]
    pf = _parse(fixture_repo, "shop/api.py")
    assert ("asyncio", "asyncio", True) in [(b.alias, b.target, b.is_module) for b in pf.imports]


def test_relative_imports() -> None:
    src = "from . import a\nfrom .b import c as d\nfrom ..e import f\nimport x.y\n"
    pf = parse_python_file("pkg/sub/mod.py", src)
    got = {(b.alias, b.target) for b in pf.imports}
    assert ("a", "pkg.sub.a") in got
    assert ("d", "pkg.sub.b.c") in got
    assert ("f", "pkg.e.f") in got
    assert ("x", "x") in got and ("x.y", "x.y") in got
    pf = parse_python_file("pkg/__init__.py", "from .core import thing\n")
    assert pf.imports[0].target == "pkg.core.thing"


def test_calls_and_local_types(fixture_repo: Path) -> None:
    units = {u.qualified_name: u for u in _parse(fixture_repo, "shop/services.py").units}
    calls = [c.raw for c in units["shop.services.OrderService.create_order"].calls]
    assert calls == ["Order", "slugify", "self.validate_order"]
    assert units["shop.services.OrderService.create_order"].local_types["order"] == "Order"
    assert units["shop.services.OrderService.validate_order"].local_types["order"] == "Order"


def test_main_guard(fixture_repo: Path) -> None:
    pf = _parse(fixture_repo, "shop/cli.py")
    assert pf.main_block_line == _line_of(fixture_repo, "shop/cli.py", 'if __name__ == "__main__":')


@pytest.mark.parametrize("rel", ["legacy/export_report.py", "shop/wip_refactor.py"])
def test_invalid_python_falls_back(fixture_repo: Path, rel: str) -> None:
    pf = _parse(fixture_repo, rel)
    assert pf.status == "syntax_error" and pf.units == [] and "Not valid Python 3" in pf.warning


def test_form_feed_does_not_shift_lines() -> None:
    src = "x = 1\n\x0c\ndef f():\n    return 1\n"
    (f,) = [u for u in parse_python_file("m.py", src).units if u.kind == "function"]
    assert (f.start_line, f.end_line) == (3, 4)


def test_crlf_source() -> None:
    src = "def f():\r\n    return 1\r\n\r\nclass A:\r\n    pass\r\n"
    units = {u.name: u for u in parse_python_file("m.py", src).units}
    assert (units["f"].start_line, units["f"].end_line) == (1, 2)
    assert (units["A"].start_line, units["A"].end_line) == (4, 5)
    assert "\r" not in units["f"].code


def test_duplicate_definitions_get_distinct_ids() -> None:
    src = "class A:\n    @property\n    def x(self):\n        return 1\n    @x.setter\n    def x(self, v):\n        pass\n"
    xs = [u for u in parse_python_file("m.py", src).units if u.name == "x"]
    assert len(xs) == 2 and xs[0].id != xs[1].id


def test_unicode_identifiers_and_lambdas() -> None:
    src = "def grüße(n):\n    f = lambda x: x + 1\n    return f(n)\n"
    (u,) = [u for u in parse_python_file("m.py", src).units if u.kind == "function"]
    assert u.name == "grüße" and u.qualified_name == "m.grüße"


@pytest.mark.parametrize(
    ("path", "module"),
    [("src/requests/api.py", "requests.api"), ("flask/__init__.py", "flask"),
     ("tests/test_x.py", "tests.test_x"), ("setup.py", "setup")],
)  # fmt: skip
def test_module_names(path: str, module: str) -> None:
    assert module_name_for(path) == module
