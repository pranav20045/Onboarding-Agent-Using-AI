"""indexed_agent tools: index search, code graph navigation and the repo overview."""

from __future__ import annotations

import contextlib

from app.core.agents.toolbox import Tool, schema
from app.core.graph.query import CodeGraph
from app.core.index.retriever import Retriever
from app.core.llm.base import ToolSpec
from app.core.repo_context import PathError, RepoContext

HIT_TEXT_CHARS = 1500


def _format_hits(hits) -> str:
    if not hits:
        return "No results."
    out = []
    for i, h in enumerate(hits, 1):
        label = f"{h.unit_kind} {h.qualified_name}" if h.qualified_name else h.kind
        text = h.text if len(h.text) <= HIT_TEXT_CHARS else h.text[:HIT_TEXT_CHARS] + "\n    ..."
        out.append(f"[{i}] {h.citation()} {label} (score {h.score:.3f})\n{text}")
    return "\n\n".join(out)


def search_tools(retriever: Retriever, seen_files: list[str] | None = None,
                 seen_symbols: list[str] | None = None) -> list[Tool]:  # fmt: skip
    """`seen_files` / `seen_symbols` collect results in rank order (for retrieval recall@k)."""

    async def _search(query: str, k: int, scope: str) -> str:
        hits = await retriever.search(query, k=max(1, min(k, 20)), scope=scope)
        if seen_files is not None:
            for h in hits:
                if h.path not in seen_files:
                    seen_files.append(h.path)
        if seen_symbols is not None:
            for h in hits:
                if h.qualified_name and h.qualified_name not in seen_symbols:
                    seen_symbols.append(h.qualified_name)
        return _format_hits(hits)

    async def search_code(query: str, k: int = 8) -> str:
        return await _search(query, k, "code")

    async def search_docs(query: str, k: int = 5) -> str:
        return await _search(query, k, "docs")

    q = {"query": {"type": "string", "description": "Natural language or identifiers"},
         "k": {"type": "integer", "description": "Number of results (default 8, max 20)"}}  # fmt: skip
    return [
        Tool(ToolSpec("search_code", "Hybrid semantic + keyword search over code units (functions, methods, "
                      "class summaries, module-level code). Returns citations and code.", schema(q, ["query"])),
             search_code, lambda a, out: f"{out.count(chr(10) + '[') + (1 if out.startswith('[') else 0)} results"),
        Tool(ToolSpec("search_docs", "Search documentation and config files (README, docs, YAML/TOML).",
                      schema(q, ["query"])),
             search_docs, lambda a, out: f"{out.count(chr(10) + '[') + (1 if out.startswith('[') else 0)} results"),
    ]  # fmt: skip


def graph_tools(graph: CodeGraph) -> list[Tool]:
    async def find_definition(symbol: str) -> str:
        refs = graph.find_definition(symbol)
        if not refs:
            return f"No definition found for {symbol!r} in the code graph (try search_code)."
        return "\n".join(f"{r.citation()} {r.kind} {r.qualified_name}: {r.signature}" for r in refs)

    def _fmt(rows: list[dict], what: str, symbol: str) -> str:
        if not rows:
            return (
                f"No {what} found for {symbol!r} (calls through dynamic dispatch may be missing)."
            )
        lines = []
        for r in rows:
            if r.get("path") and r.get("start_line"):
                loc = f"[{r['path']}:{r['start_line']}-{r['end_line']}]"
            else:
                loc = "(unresolved)" if r.get("kind") == "unresolved" else f"({r.get('kind')})"
            call = f" call at line {r['call_line']}" if r.get("call_line") else ""
            lines.append(f"depth {r['depth']}: {r['qualified_name']} {loc}{call} via {r['via']}")
        return "\n".join(lines)

    async def find_callers(symbol: str, depth: int = 1) -> str:
        return _fmt(graph.find_callers(symbol, depth), "callers", symbol)

    async def find_callees(symbol: str, depth: int = 1) -> str:
        return _fmt(graph.find_callees(symbol, depth), "callees", symbol)

    sym = {
        "symbol": {"type": "string", "description": "Name or qualified name, e.g. Session.request"}
    }
    depth = {"depth": {"type": "integer", "description": "Hops to follow (1-5, default 1)"}}
    return [
        Tool(ToolSpec("find_definition", "Locate where a class/function/method is defined (code graph).",
                      schema(sym, ["symbol"])), find_definition),
        Tool(ToolSpec("find_callers", "Functions that call the given symbol (static call graph, best effort).",
                      schema({**sym, **depth}, ["symbol"])), find_callers,
             lambda a, out: f"{0 if out.startswith('No ') else len(out.splitlines())} callers"),
        Tool(ToolSpec("find_callees", "Functions called by the given symbol (static call graph, best effort; "
                      "unresolved calls are listed by name).", schema({**sym, **depth}, ["symbol"])), find_callees,
             lambda a, out: f"{0 if out.startswith('No ') else len(out.splitlines())} callees"),
    ]  # fmt: skip


def overview_tool(ctx: RepoContext, graph: CodeGraph | None) -> Tool:
    async def repo_overview() -> str:
        parts: list[str] = []
        top = ctx.list_dir("")
        parts.append("Top-level entries: " + ", ".join(top))
        readme = next((p for p in top if p.lower().startswith("readme")), None)
        if readme:
            try:
                body, s, e, total = ctx.read_range(readme, 1, 40)
                parts.append(f"{readme} (first {e} of {total} lines):\n{body}")
            except (PathError, FileNotFoundError, ValueError):
                pass
        if graph is not None:
            pyproject = None
            with contextlib.suppress(PathError, FileNotFoundError):
                pyproject = "\n".join(ctx.lines("pyproject.toml"))
            eps = graph.entry_points(pyproject)[:25]
            if eps:
                parts.append("Entry points:\n" + "\n".join(
                    f"- {e.kind}: {e.detail} [{e.path}:{e.start_line}-{e.end_line}]" if e.path else f"- {e.kind}: {e.detail}"
                    for e in eps))  # fmt: skip
            central = graph.centrality(top=15)
            if central:
                parts.append("Most central symbols (call/inherit PageRank):\n" + "\n".join(
                    f"- {r.kind} {r.qualified_name} {r.citation()}" for r, _ in central))  # fmt: skip
            s = graph.summary()
            parts.append(
                f"Graph: {s['nodes']}; call resolution {s['resolution'].get('resolution_rate')}"
            )
        return "\n\n".join(parts)

    return Tool(ToolSpec("repo_overview", "Overview of the repository: layout, README start, entry points "
                         "and the most central classes/functions.", schema({}, [])), repo_overview,
                lambda a, out: "overview")  # fmt: skip
