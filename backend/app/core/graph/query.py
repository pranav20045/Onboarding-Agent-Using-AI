"""Query and persistence layer over the code graph (used by agent tools and the tour)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import networkx as nx

ROUTE_DECORATOR_RE = re.compile(
    r"(^|\.)(route|get|post|put|patch|delete|head|options|websocket|api_route)\s*\("
)
CLI_DECORATOR_RE = re.compile(r"(^|\.)(command|group)\s*(\(|$)")
_CONSOLE_SCRIPT_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*([A-Za-z0-9_.]+):([A-Za-z0-9_.]+)")


@dataclass
class SymbolRef:
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str = ""

    def citation(self) -> str:
        return f"[{self.path}:{self.start_line}-{self.end_line}]"


@dataclass
class EntryPoint:
    kind: str  # main_block | route | cli | console_script
    detail: str
    path: str
    start_line: int
    end_line: int
    qualified_name: str | None = None


class CodeGraph:
    def __init__(self, graph: nx.MultiDiGraph, stats: dict | None = None) -> None:
        self.g = graph
        self.stats = stats or {}
        self._by_name: dict[str, list[str]] = {}
        for node, data in graph.nodes(data=True):
            if node.startswith("sym:"):
                self._by_name.setdefault(data["name"], []).append(node)

    # -- persistence -------------------------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stats": self.stats, "graph": nx.node_link_data(self.g, edges="edges")}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> CodeGraph:
        payload = json.loads(path.read_text(encoding="utf-8"))
        g = nx.node_link_graph(payload["graph"], directed=True, multigraph=True, edges="edges")
        return cls(g, payload.get("stats"))

    # -- helpers -----------------------------------------------------------------------------
    def _ref(self, node: str) -> SymbolRef:
        d = self.g.nodes[node]
        return SymbolRef(
            qualified_name=d.get("qualified_name", d.get("name", node)),
            kind=d.get("kind", "?"),
            path=d.get("path", ""),
            start_line=d.get("start_line", 1),
            end_line=d.get("end_line", 1),
            signature=d.get("signature", ""),
        )

    def resolve_symbol(self, symbol: str) -> list[str]:
        """Exact qualified name, else dotted-suffix match (`Class.method`, `method`)."""
        symbol = symbol.strip().strip("`").removesuffix("()")
        exact = f"sym:{symbol}"
        if exact in self.g:
            return [exact]
        last = symbol.rsplit(".", 1)[-1]
        return sorted(
            n for n in self._by_name.get(last, [])
            if n == f"sym:{symbol}" or n.endswith(f".{symbol}")
        )  # fmt: skip

    # -- queries used by agent tools -------------------------------------------------------
    def find_definition(self, symbol: str, limit: int = 10) -> list[SymbolRef]:
        return [self._ref(n) for n in self.resolve_symbol(symbol)[:limit]]

    def _walk(self, symbol: str, depth: int, reverse: bool) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        frontier = [(n, 0) for n in self.resolve_symbol(symbol)]
        seen.update(n for n, _ in frontier)
        while frontier:
            node, d = frontier.pop(0)
            if d >= depth:
                continue
            edges = (
                self.g.in_edges(node, data=True) if reverse else self.g.out_edges(node, data=True)
            )
            for src, dst, data in edges:
                if data.get("kind") not in ("calls", "unresolved_call"):
                    continue
                other = src if reverse else dst
                if other in seen:
                    continue
                seen.add(other)
                entry = {
                    "depth": d + 1,
                    "call_line": data.get("line"),
                    "via": node.removeprefix("sym:"),
                }
                if other.startswith("sym:"):
                    entry.update(asdict(self._ref(other)))
                    frontier.append((other, d + 1))
                elif other.startswith("mod:"):
                    od = self.g.nodes[other]
                    entry.update(qualified_name=od["name"], kind="module", path=od.get("path", ""))
                else:
                    entry.update(qualified_name=self.g.nodes[other].get("name", other),
                                 kind=self.g.nodes[other].get("kind", "?"))  # fmt: skip
                results.append(entry)
        return results

    def find_callers(self, symbol: str, depth: int = 1) -> list[dict]:
        return self._walk(symbol, max(1, min(depth, 5)), reverse=True)

    def find_callees(self, symbol: str, depth: int = 1) -> list[dict]:
        return self._walk(symbol, max(1, min(depth, 5)), reverse=False)

    def get_neighbors(self, node_or_symbol: str) -> dict[str, list[dict]]:
        nodes = (
            [node_or_symbol] if node_or_symbol in self.g else self.resolve_symbol(node_or_symbol)
        )
        out: dict[str, list[dict]] = {"outgoing": [], "incoming": []}
        for node in nodes[:3]:
            for _, dst, data in self.g.out_edges(node, data=True):
                out["outgoing"].append({"from": node, "to": dst, **data})
            for src, _, data in self.g.in_edges(node, data=True):
                out["incoming"].append({"from": src, "to": node, **data})
        return out

    def entry_points(
        self, pyproject_text: str | None = None, setup_text: str | None = None
    ) -> list[EntryPoint]:
        eps: list[EntryPoint] = []
        for node, d in self.g.nodes(data=True):
            if d.get("kind") == "module" and d.get("main_block_line"):
                eps.append(EntryPoint("main_block", f"`if __name__ == '__main__'` in {d['name']}",
                                      d["path"], d["main_block_line"], d["main_block_line"]))  # fmt: skip
            if node.startswith("sym:") and d.get("decorators"):
                for dec in d["decorators"]:
                    kind = None
                    if ROUTE_DECORATOR_RE.search(dec):
                        kind = "route"
                    elif CLI_DECORATOR_RE.search(dec):
                        kind = "cli"
                    if kind:
                        eps.append(EntryPoint(kind, f"@{dec}", d["path"], d["start_line"],
                                              d["end_line"], d["qualified_name"]))  # fmt: skip
                        break
        for name, target in _console_scripts(pyproject_text, setup_text):
            module, _, func = target.partition(":")
            refs = self.find_definition(f"{module}.{func}")
            if refs:
                r = refs[0]
                eps.append(EntryPoint("console_script", f"{name} = {target}", r.path,
                                      r.start_line, r.end_line, r.qualified_name))  # fmt: skip
            else:
                eps.append(EntryPoint("console_script", f"{name} = {target}", "", 0, 0))
        return sorted(eps, key=lambda e: (e.kind, e.path, e.start_line))

    def centrality(self, top: int = 15) -> list[tuple[SymbolRef, float]]:
        """Classes/functions ranked by PageRank over call + inherit edges (for the tour)."""
        sub = nx.DiGraph()
        for src, dst, data in self.g.edges(data=True):
            if (
                data.get("kind") in ("calls", "inherits")
                and src.startswith("sym:")
                and dst.startswith("sym:")
            ):
                sub.add_edge(src, dst)
        if sub.number_of_nodes() == 0:
            return []
        ranks = _pagerank(sub)
        ordered = sorted(ranks.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        return [(self._ref(n), score) for n, score in ordered]

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for _, d in self.g.nodes(data=True):
            kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
        edges: dict[str, int] = {}
        for _, _, d in self.g.edges(data=True):
            edges[d.get("kind", "?")] = edges.get(d.get("kind", "?"), 0) + 1
        return {"nodes": kinds, "edges": edges, "resolution": self.stats}


def _console_scripts(pyproject_text: str | None, setup_text: str | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if pyproject_text:
        try:
            data = tomllib.loads(pyproject_text)
        except tomllib.TOMLDecodeError:
            data = {}
        scripts = data.get("project", {}).get("scripts", {}) or {}
        scripts.update(data.get("tool", {}).get("poetry", {}).get("scripts", {}) or {})
        found += [(k, v) for k, v in scripts.items() if isinstance(v, str) and ":" in v]
    if setup_text:  # parsed as text only, never executed
        for line in setup_text.splitlines():
            m = _CONSOLE_SCRIPT_RE.match(line.strip().strip("\"',[]"))
            if m:
                found.append((m.group(1), f"{m.group(2)}:{m.group(3)}"))
    return found


def _pagerank(
    g: nx.DiGraph, alpha: float = 0.85, iters: int = 100, tol: float = 1e-10
) -> dict[str, float]:
    """Plain power-iteration PageRank (networkx's version needs scipy, which we don't ship)."""
    nodes = list(g.nodes)
    n = len(nodes)
    rank = dict.fromkeys(nodes, 1.0 / n)
    out_deg = {v: g.out_degree(v) for v in nodes}
    for _ in range(iters):
        dangling = alpha * sum(rank[v] for v in nodes if out_deg[v] == 0) / n
        new = dict.fromkeys(nodes, (1.0 - alpha) / n + dangling)
        for v in nodes:
            if out_deg[v]:
                share = alpha * rank[v] / out_deg[v]
                for w in g.successors(v):
                    new[w] += share
        delta = sum(abs(new[v] - rank[v]) for v in nodes)
        rank = new
        if delta < tol:
            break
    return rank
