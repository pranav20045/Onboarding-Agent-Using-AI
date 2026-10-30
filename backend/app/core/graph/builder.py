"""Build the code graph from parsed files, with best-effort static call resolution.

Nodes (id prefix -> meaning):
  file:<path>        a source file
  mod:<module>       a Python module
  sym:<qualname>     a class / function / method
  ext:<dotted>       something imported from outside the repo (stdlib, third-party)
  unres:<raw>        an unresolved call target, keyed by the raw callee expression

Edges carry `kind` in {defines, imports, calls, unresolved_call, inherits} and, for calls,
the call line. A call is classified as exactly one of:
  resolved    -> `calls` edge to a sym: node
  builtin     -> dropped (len, print, ...), counted in stats only
  external    -> `calls` edge to an ext: node (e.g. `re.sub`, `functools.wraps`)
  unresolved  -> `unresolved_call` edge; the agent falls back to text search for these

Resolution handles: names defined in the same module, nested definitions in enclosing
functions, imports (incl. `from x import y as z`, relative imports, `import a.b`),
`self.m()` / `cls.m()` / `super().m()` through the in-repo class hierarchy, `Class.m()`
and `module.func()`. It does not attempt type inference (`obj.method()` on a local variable),
dynamic dispatch, `getattr`, or dependency injection.
"""

from __future__ import annotations

import builtins
from collections import Counter
from dataclasses import dataclass, field

import networkx as nx

from app.core.models import CodeUnit, ImportBinding, ParsedFile

BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass
class ResolutionStats:
    resolved: int = 0
    unresolved: int = 0
    external: int = 0
    builtin: int = 0
    unresolved_examples: Counter[str] = field(default_factory=Counter)
    # Same counts restricted to non-test code (tests are dominated by pytest fixtures).
    nontest_resolved: int = 0
    nontest_unresolved: int = 0

    @property
    def total(self) -> int:
        return self.resolved + self.unresolved + self.external + self.builtin

    @property
    def resolution_rate(self) -> float | None:
        """Resolved share of calls that could point into the repo (excludes builtins/external)."""
        denom = self.resolved + self.unresolved
        return self.resolved / denom if denom else None

    def as_dict(self) -> dict:
        rate = self.resolution_rate
        return {
            "calls_total": self.total,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "external": self.external,
            "builtin": self.builtin,
            "resolution_rate": round(rate, 4) if rate is not None else None,
            "resolution_rate_non_test": (
                round(self.nontest_resolved / (self.nontest_resolved + self.nontest_unresolved), 4)
                if self.nontest_resolved + self.nontest_unresolved
                else None
            ),
            "top_unresolved": [[k, v] for k, v in self.unresolved_examples.most_common(15)],
        }


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test", "testing") for p in parts[:-1]) or parts[-1].startswith(
        ("test_", "conftest")
    )


def sym(qname: str) -> str:
    return f"sym:{qname}"


class _Resolver:
    def __init__(self, parsed_files: list[ParsedFile]) -> None:
        self.units: dict[str, CodeUnit] = {}
        self.modules: dict[str, ParsedFile] = {}
        for pf in parsed_files:
            if pf.status == "syntax_error":
                continue
            self.modules[pf.module] = pf
            for u in pf.units:
                self.units.setdefault(u.qualified_name, u)
        self.imports: dict[str, dict[str, ImportBinding]] = {
            m: {b.alias: b for b in pf.imports if b.alias != "*"} for m, pf in self.modules.items()
        }
        self.star_imports: dict[str, list[str]] = {
            m: [b.target for b in pf.imports if b.alias == "*"] for m, pf in self.modules.items()
        }
        self.unit_module: dict[str, str] = {
            u.qualified_name: pf.module for pf in self.modules.values() for u in pf.units
        }
        self.class_bases: dict[str, list[str]] = {}
        for qn, u in self.units.items():
            if u.kind == "class":
                mod = self.unit_module[qn]
                resolved = []
                for raw in u.bases:
                    target = self.resolve_dotted(raw, mod, scope=u.parent)
                    if target and target in self.units and self.units[target].kind == "class":
                        resolved.append(target)
                self.class_bases[qn] = resolved

    # -- name lookup -------------------------------------------------------------------------
    def canonical(self, dotted: str, depth: int = 0) -> str | None:
        """Map an import target like `pkg.mod.name` to an in-repo qualified name, if any.

        Follows re-exports (`from .mod import name` in a package `__init__`) a few levels deep.
        """
        if dotted in self.units:
            return dotted
        if dotted in self.modules:
            return dotted
        if depth > 4 or "." not in dotted:
            return None
        mod, _, name = dotted.rpartition(".")
        if mod in self.modules:
            binding = self.imports[mod].get(name)
            if binding is not None:
                return self.canonical(binding.target, depth + 1)
            for star in self.star_imports.get(mod, []):
                hit = self.canonical(f"{star}.{name}", depth + 1)
                if hit:
                    return hit
            return None
        head = self.canonical(mod, depth + 1)
        if head and head != mod:
            return self.canonical(f"{head}.{name}", depth + 1)
        return None

    def resolve_dotted(self, raw: str, module: str, scope: str | None) -> str | None:
        """Resolve a dotted name as written inside `scope` (a qualname) of `module`."""
        head, _, rest = raw.partition(".")
        # 1. Enclosing function/class scopes (nested definitions), innermost first.
        s = scope
        while s and s != module:
            cand = f"{s}.{head}"
            if cand in self.units and self.units[cand].parent == s:
                return self._descend(cand, rest)
            s = self.units[s].parent if s in self.units else None
        # 2. Module-level definitions.
        cand = f"{module}.{head}"
        if cand in self.units:
            return self._descend(cand, rest)
        # 3. Imports.
        binding = self.imports.get(module, {}).get(head)
        if binding is not None:
            target = f"{binding.target}.{rest}" if rest else binding.target
            hit = self.canonical(target)
            if hit:
                return hit
            if rest and binding.is_module:  # `import a` then `a.b.c()`
                return self.canonical(f"{binding.target}.{rest}")
            return None
        for star in self.star_imports.get(module, []):
            hit = self.canonical(f"{star}.{raw}")
            if hit:
                return hit
        return None

    def _descend(self, qname: str, rest: str) -> str | None:
        if not rest:
            return qname
        target = f"{qname}.{rest}"
        if target in self.units:
            return target
        if self.units.get(qname) is not None and self.units[qname].kind == "class":
            head, _, tail = rest.partition(".")
            found = self.lookup_method(qname, head)
            return self._descend(found, tail) if found and tail else found
        return None

    def lookup_method(self, cls: str, name: str, seen: set[str] | None = None) -> str | None:
        """Find `name` on class `cls` or its in-repo bases (depth-first, like a simple MRO)."""
        seen = seen or set()
        if cls in seen:
            return None
        seen.add(cls)
        cand = f"{cls}.{name}"
        if cand in self.units:
            return cand
        for base in self.class_bases.get(cls, []):
            hit = self.lookup_method(base, name, seen)
            if hit:
                return hit
        return None

    def enclosing_class(self, unit: CodeUnit) -> str | None:
        parent = self.units.get(unit.parent or "")
        if unit.kind == "method" and parent is not None and parent.kind == "class":
            return parent.qualified_name
        return None

    def _in_repo_root(self, dotted: str) -> bool:
        root = dotted.split(".")[0]
        return any(m == root or m.startswith(root + ".") for m in self.modules)

    def external_target(self, dotted: str, depth: int = 0) -> str | None:
        """Follow re-exports through in-repo modules until the name leaves the repo.

        e.g. `requests.compat.urlparse` -> `urllib.parse.urlparse`.
        """
        if not self._in_repo_root(dotted):
            return dotted
        if depth > 4 or "." not in dotted:
            return None
        mod, _, name = dotted.rpartition(".")
        if mod in self.modules:
            binding = self.imports[mod].get(name)
            if binding is not None:
                return self.external_target(binding.target, depth + 1)
        return None

    def is_external(self, raw: str, module: str) -> str | None:
        head, _, rest = raw.partition(".")
        binding = self.imports.get(module, {}).get(head)
        if binding is None:
            return None
        target = self.external_target(binding.target)
        if target is None:
            return None
        return f"{target}.{rest}" if rest else target

    def _class_of(self, type_expr: str, unit: CodeUnit) -> str | None:
        """Resolve a constructor / annotation expression to an in-repo class qualname."""
        module = self.unit_module[unit.qualified_name]
        hit = self.resolve_dotted(type_expr, module, unit.parent)
        if hit and hit in self.units and self.units[hit].kind == "class":
            return hit
        return None

    def attr_type(self, cls: str, attr: str) -> str | None:
        """Type of `self.<attr>` from assignments `self.attr = Cls(...)` in any method."""
        for u in self.units.values():
            if u.parent == cls and u.kind == "method" and f"self.{attr}" in u.local_types:
                found = self._class_of(u.local_types[f"self.{attr}"], u)
                if found:
                    return found
        for base in self.class_bases.get(cls, []):
            found = self.attr_type(base, attr)
            if found:
                return found
        return None

    def resolve_call(self, raw: str, unit: CodeUnit) -> tuple[str, str]:
        """Return (category, target) for a call made inside `unit`."""
        module = self.unit_module[unit.qualified_name]
        scope = unit.qualified_name
        head, _, rest = raw.partition(".")
        cls = self.enclosing_class(unit)
        if head in ("self", "cls") and cls and rest:
            name, _, tail = rest.partition(".")
            if not tail:
                hit = self.lookup_method(cls, name)
                if hit:
                    return "resolved", hit
            elif "." not in tail:  # self.attr.method()
                attr_cls = self.attr_type(cls, name)
                hit = self.lookup_method(attr_cls, tail) if attr_cls else None
                if hit:
                    return "resolved", hit
            return "unresolved", raw
        if rest and head in unit.local_types and "." not in rest:  # x = Cls(); x.method()
            var_cls = self._class_of(unit.local_types[head], unit)
            hit = self.lookup_method(var_cls, rest) if var_cls else None
            if hit:
                return "resolved", hit
        if head == "super()" and cls and rest:
            for base in self.class_bases.get(cls, []):
                hit = self.lookup_method(base, rest)
                if hit:
                    return "resolved", hit
            return "unresolved", raw
        hit = self.resolve_dotted(raw, module, scope)
        if hit and hit in self.units:
            return "resolved", hit
        if hit and hit in self.modules:
            return "unresolved", raw  # calling a module object makes no sense statically
        ext = self.is_external(raw, module)
        if ext:
            return "external", ext
        if "." not in raw and raw in BUILTIN_NAMES:
            return "builtin", raw
        return "unresolved", raw


def build_graph(parsed_files: list[ParsedFile]) -> tuple[nx.MultiDiGraph, ResolutionStats]:
    g = nx.MultiDiGraph()
    r = _Resolver(parsed_files)
    stats = ResolutionStats()

    for pf in parsed_files:
        g.add_node(f"file:{pf.path}", kind="file", path=pf.path, status=pf.status)
        if pf.status == "syntax_error":
            continue
        mod_node = f"mod:{pf.module}"
        g.add_node(mod_node, kind="module", name=pf.module, path=pf.path,
                   main_block_line=pf.main_block_line)  # fmt: skip
        g.add_edge(f"file:{pf.path}", mod_node, kind="defines")
        for u in pf.units:
            if u.kind == "module":
                continue
            g.add_node(
                sym(u.qualified_name), kind=u.kind, name=u.name, qualified_name=u.qualified_name,
                path=u.path, start_line=u.start_line, end_line=u.end_line,
                signature=u.signature, decorators=list(u.decorators), size=u.end_line - u.start_line + 1,
            )  # fmt: skip
            parent = mod_node if u.parent == pf.module else sym(u.parent or "")
            g.add_edge(parent, sym(u.qualified_name), kind="defines")

    for pf in parsed_files:
        if pf.status == "syntax_error":
            continue
        mod_node = f"mod:{pf.module}"
        for b in pf.imports:
            target = r.canonical(b.target)
            if target is None:
                ext_root = b.target.split(".")[0]
                if any(m == ext_root or m.startswith(ext_root + ".") for m in r.modules):
                    continue  # in-repo name we could not pin down (e.g. a variable)
                node = f"ext:{b.target}"
                g.add_node(node, kind="external", name=b.target)
            else:
                node = f"mod:{target}" if target in r.modules else sym(target)
            g.add_edge(mod_node, node, kind="imports", line=b.line)

        for u in pf.units:
            src = mod_node if u.kind == "module" else sym(u.qualified_name)
            if u.kind == "class":
                for base in r.class_bases.get(u.qualified_name, []):
                    g.add_edge(src, sym(base), kind="inherits")
            for call in u.calls:
                category, target = r.resolve_call(call.raw, u)
                if not is_test_path(pf.path):
                    stats.nontest_resolved += category == "resolved"
                    stats.nontest_unresolved += category == "unresolved"
                if category == "resolved":
                    stats.resolved += 1
                    g.add_edge(src, sym(target), kind="calls", line=call.line)
                elif category == "external":
                    stats.external += 1
                    g.add_node(f"ext:{target}", kind="external", name=target)
                    g.add_edge(src, f"ext:{target}", kind="calls", line=call.line)
                elif category == "builtin":
                    stats.builtin += 1
                else:
                    stats.unresolved += 1
                    stats.unresolved_examples[call.raw] += 1
                    node = f"unres:{call.raw}"
                    g.add_node(node, kind="unresolved", name=call.raw)
                    g.add_edge(src, node, kind="unresolved_call", line=call.line, raw=call.raw)
    return g, stats
