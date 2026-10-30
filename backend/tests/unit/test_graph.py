from __future__ import annotations

from pathlib import Path

import pytest

from app.core.graph.builder import build_graph
from app.core.graph.query import CodeGraph
from app.core.ingest.walk import walk_repo
from app.core.parse.python_parser import parse_python_file


@pytest.fixture(scope="module")
def graph(fixture_repo: Path) -> CodeGraph:
    files = [f for f in walk_repo(fixture_repo).files if f.language == "python"]
    g, stats = build_graph([parse_python_file(f.path, f.text) for f in files])
    return CodeGraph(g, stats.as_dict())


def _calls(graph: CodeGraph, src: str) -> set[str]:
    return {
        dst.removeprefix("sym:")
        for _, dst, d in graph.g.out_edges(f"sym:{src}", data=True)
        if d["kind"] == "calls"
    }


def test_resolved_calls(graph: CodeGraph) -> None:
    create = _calls(graph, "shop.services.OrderService.create_order")
    assert create == {
        "shop.models.Order",  # constructor -> class
        "shop.text_utils.slugify",  # import updated after the rename
        "shop.services.OrderService.validate_order",  # self.method()
    }
    assert "shop.payments.charge" in _calls(graph, "shop.services.OrderService.checkout")
    # super().__init__() through the class hierarchy
    assert _calls(graph, "shop.models.Product.__init__") == {"shop.models.Entity.__init__"}


def test_annotated_parameter_type_resolution() -> None:
    from app.core.parse.python_parser import parse_python_file as parse

    a = parse("pkg/a.py", "class Box:\n    def size(self):\n        return 1\n")
    b = parse(
        "pkg/b.py", "from pkg.a import Box\n\ndef measure(box: Box):\n    return box.size()\n"
    )
    g, _ = build_graph([a, b])
    targets = {
        dst for _, dst, d in g.out_edges("sym:pkg.b.measure", data=True) if d["kind"] == "calls"
    }
    assert targets == {"sym:pkg.a.Box.size"}


def test_aliased_import_decorator_call(graph: CodeGraph) -> None:
    targets = {
        dst
        for _, dst, d in graph.g.out_edges("mod:shop.payments", data=True)
        if d["kind"] == "calls"
    }
    assert "sym:shop.utils.retry" in targets


def test_module_level_instance_method(graph: CodeGraph) -> None:
    targets = {
        dst for _, dst, d in graph.g.out_edges("mod:shop.api", data=True) if d["kind"] == "calls"
    }
    assert "sym:shop.api.Router.post" in targets  # router = Router(); router.post(...)


def test_unresolved_and_external(graph: CodeGraph) -> None:
    edges = list(graph.g.out_edges("sym:shop.api.create_order_route", data=True))
    unresolved = {d["raw"] for _, _, d in edges if d["kind"] == "unresolved_call"}
    assert "service.create_order" in unresolved
    ext = {dst for _, dst, d in graph.g.out_edges("sym:shop.text_utils.slugify", data=True)}
    assert "ext:re.sub" in ext


def test_inheritance(graph: CodeGraph) -> None:
    assert graph.g.has_edge("sym:shop.models.DigitalProduct", "sym:shop.models.Product")
    kinds = [
        d["kind"]
        for d in graph.g.get_edge_data("sym:shop.models.Product", "sym:shop.models.Entity").values()
    ]
    assert kinds == ["inherits"]


def test_stats_add_up(graph: CodeGraph) -> None:
    s = graph.stats
    assert s["calls_total"] == s["resolved"] + s["unresolved"] + s["external"] + s["builtin"]
    assert s["resolution_rate"] == round(s["resolved"] / (s["resolved"] + s["unresolved"]), 4)
    n_unres_edges = sum(1 for *_, d in graph.g.edges(data=True) if d["kind"] == "unresolved_call")
    assert n_unres_edges == s["unresolved"]


def test_find_definition(graph: CodeGraph) -> None:
    (ref,) = graph.find_definition("OrderService.validate_order")
    assert ref.path == "shop/services.py" and ref.kind == "method"
    assert {r.qualified_name for r in graph.find_definition("describe")} == {
        "shop.models.Entity.describe",
        "shop.models.Product.describe",
    }
    assert graph.find_definition("does_not_exist") == []
    assert graph.find_definition("shop.services.OrderService")[0].kind == "class"


def test_callers_and_callees(graph: CodeGraph) -> None:
    callers = graph.find_callers("validate_order")
    assert [c["qualified_name"] for c in callers] == ["shop.services.OrderService.create_order"]
    deep = graph.find_callers("validate_order", depth=2)
    assert "shop.api.create_order_route" not in {
        c["qualified_name"] for c in deep
    }  # unresolved hop
    callees = graph.find_callees("OrderService.checkout", depth=2)
    names = {c["qualified_name"] for c in callees}
    assert "shop.payments.charge" in names


def test_entry_points(graph: CodeGraph, fixture_repo: Path) -> None:
    eps = graph.entry_points((fixture_repo / "pyproject.toml").read_text(encoding="utf-8"))
    kinds = {(e.kind, e.qualified_name) for e in eps}
    assert ("route", "shop.api.create_order_route") in kinds
    assert ("route", "shop.api.fetch_inventory") in kinds
    assert ("console_script", "shop.cli.main") in kinds
    assert any(e.kind == "main_block" and e.path == "shop/cli.py" for e in eps)


def test_save_load_roundtrip(graph: CodeGraph, tmp_path: Path) -> None:
    graph.save(tmp_path / "g.json")
    loaded = CodeGraph.load(tmp_path / "g.json")
    assert sorted(loaded.g.nodes) == sorted(graph.g.nodes)
    assert loaded.g.number_of_edges() == graph.g.number_of_edges()
    assert loaded.stats == graph.stats
    assert loaded.find_callers("validate_order") == graph.find_callers("validate_order")


def test_centrality(graph: CodeGraph) -> None:
    ranked = graph.centrality(top=5)
    assert ranked and all(score > 0 for _, score in ranked)
