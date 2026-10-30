"""Extract code units, imports and call sites from Python source with tree-sitter.

Validity is decided by Python's own `ast.parse` (parse only, never executed), because the
tree-sitter grammar is error-tolerant and accepts Python 2 `print` statements without flagging
an error. Files that are not valid Python 3 are returned with status `syntax_error` and no
structural units; the indexer then falls back to plain-text chunks for them.
"""

from __future__ import annotations

import ast
import warnings
from collections import Counter
from functools import lru_cache
from pathlib import PurePosixPath

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from app.core.models import (
    CallSite,
    CodeUnit,
    ImportBinding,
    ParsedFile,
    sha256_hex,
    split_lines,
    stable_id,
)

_FUNC_TYPES = {"function_definition"}
_CLASS_TYPES = {"class_definition"}


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_python.language()))


def module_name_for(path: str) -> str:
    """`src/pkg/mod.py` -> `pkg.mod`; `pkg/__init__.py` -> `pkg`."""
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] in {"src", "lib"} and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else p.stem


def is_valid_python3(text: str) -> tuple[bool, str | None]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # SyntaxWarning for invalid escapes etc.
            ast.parse(text)
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"
    except (ValueError, RecursionError, MemoryError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _docstring(body: Node | None) -> str | None:
    if body is None:
        return None
    for child in body.named_children:
        if child.type == "comment":
            continue
        if child.type == "expression_statement" and child.named_children:
            expr = child.named_children[0]
            if expr.type == "string":
                raw = _text(expr)
                try:
                    value = ast.literal_eval(raw)  # string literal only; safe
                except (ValueError, SyntaxError):
                    return raw.strip("\"'")
                return value.strip() if isinstance(value, str) else None
        return None
    return None


def _callee(node: Node) -> str | None:
    """Raw dotted callee for identifiers/attribute chains; e.g. `self.a.b`, `super().m`."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is None or attr is None:
            return None
        base = _callee(obj)
        if base is None and obj.type == "call":
            fn = obj.child_by_field_name("function")
            if fn is not None and _text(fn) == "super":
                base = "super()"
        return f"{base}.{_text(attr)}" if base else None
    return None


class _Extractor:
    def __init__(self, path: str, module: str, source: bytes, lines: list[str]) -> None:
        self.path = path
        self.module = module
        self.source = source
        self.lines = lines
        self.units: list[CodeUnit] = []
        self.imports: list[ImportBinding] = []
        self.main_block_line: int | None = None
        self._occurrences: Counter[str] = Counter()

    # -- helpers -------------------------------------------------------------------------------
    def _code(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1 : end])

    def _make_id(self, qname: str, kind: str) -> str:
        key = f"{kind}:{qname}"
        self._occurrences[key] += 1
        return stable_id(self.path, key, str(self._occurrences[key]))

    # -- traversal -----------------------------------------------------------------------------
    def run(self, root: Node) -> None:
        end_line = max(len(self.lines), 1)
        module_unit = CodeUnit(
            id=self._make_id(self.module, "module"),
            path=self.path,
            kind="module",
            name=self.module.rsplit(".", 1)[-1],
            qualified_name=self.module,
            parent=None,
            start_line=1,
            end_line=end_line,
            signature=f"module {self.module}",
            docstring=_docstring(root),
            decorators=[],
            code=self._code(1, end_line),
            content_hash=sha256_hex(self.source),
        )
        self.units.append(module_unit)
        self._visit_block(root, module_unit, scope="module")

    def _visit_block(self, block: Node, owner: CodeUnit, scope: str) -> None:
        """Visit statements of a module/class/function body. `owner` receives call sites."""
        for child in block.named_children:
            self._visit_statement(child, owner, scope)

    def _visit_statement(self, node: Node, owner: CodeUnit, scope: str) -> None:
        t = node.type
        if t == "decorated_definition":
            definition = node.child_by_field_name("definition")
            decorators = [
                _text(d).lstrip("@").strip() for d in node.named_children if d.type == "decorator"
            ]
            for d in node.named_children:
                if d.type == "decorator":
                    self._collect_calls(d, owner)
            if definition is not None:
                self._visit_definition(definition, owner, scope, decorators, outer=node)
            return
        if t in _FUNC_TYPES or t in _CLASS_TYPES:
            self._visit_definition(node, owner, scope, [], outer=node)
            return
        if t in ("import_statement", "import_from_statement"):
            self._record_import(node)
            return
        if t == "if_statement" and scope == "module" and self._is_main_guard(node):
            self.main_block_line = node.start_point.row + 1
        # Compound statements may contain nested definitions (e.g. `if TYPE_CHECKING:` imports,
        # conditional function definitions) -- descend into blocks, collect calls elsewhere.
        self._descend(node, owner, scope)

    def _descend(self, node: Node, owner: CodeUnit, scope: str) -> None:
        for child in node.named_children:
            if child.type == "block":
                self._visit_block(child, owner, scope)
            elif child.type in (
                "else_clause",
                "elif_clause",
                "except_clause",
                "finally_clause",
                "case_clause",
                "with_clause",
            ) or child.type.endswith("_clause"):
                self._descend(child, owner, scope)
            elif child.type in (
                "decorated_definition",
                "function_definition",
                "class_definition",
                "import_statement",
                "import_from_statement",
            ):
                self._visit_statement(child, owner, scope)
            else:
                self._collect_calls(child, owner)  # fmt: skip

    def _collect_calls(self, node: Node, owner: CodeUnit) -> None:
        """Record call sites under `node`, without entering nested definitions or lambdas' defs."""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in _FUNC_TYPES or n.type in _CLASS_TYPES or n.type == "decorated_definition":
                continue
            if n.type == "call":
                fn = n.child_by_field_name("function")
                raw = _callee(fn) if fn is not None else None
                if raw:
                    owner.calls.append(CallSite(raw=raw, line=n.start_point.row + 1))
            elif n.type == "assignment":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                if left is not None and right is not None and right.type == "call":
                    fn = right.child_by_field_name("function")
                    raw = _callee(fn) if fn is not None else None
                    target = _text(left)
                    if raw and (left.type == "identifier" or target.startswith("self.")):
                        owner.local_types[target] = raw
            stack.extend(reversed(n.named_children))

    @staticmethod
    def _annotated_params(params: Node) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in params.named_children:
            if p.type not in ("typed_parameter", "typed_default_parameter"):
                continue
            name = p.child_by_field_name("name")
            if name is None:
                name = next((c for c in p.named_children if c.type == "identifier"), None)
            type_node = p.child_by_field_name("type")
            if name is None or type_node is None:
                continue
            inner = type_node.named_children[0] if type_node.named_children else type_node
            if inner.type in ("identifier", "attribute"):
                out[_text(name)] = _text(inner)
        return out

    def _visit_definition(
        self, node: Node, owner: CodeUnit, scope: str, decorators: list[str], outer: Node
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:
            return
        name = _text(name_node)
        is_class = node.type in _CLASS_TYPES
        if is_class:
            kind = "class"
        elif scope == "class":
            kind = "method"
        else:
            kind = "function"
        qname = f"{owner.qualified_name}.{name}"
        start = outer.start_point.row + 1
        end = node.end_point.row + 1
        header_end = body.start_byte if body is not None else node.end_byte
        signature = self.source[node.start_byte : header_end].decode("utf-8", "replace")
        signature = " ".join(signature.split()).rstrip(":").strip()
        code = self._code(start, end)
        bases: list[str] = []
        if is_class:
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                bases = [
                    _text(a) for a in supers.named_children if a.type in ("identifier", "attribute")
                ]
        unit = CodeUnit(
            id=self._make_id(qname, kind),
            path=self.path,
            kind=kind,  # type: ignore[arg-type]
            name=name,
            qualified_name=qname,
            parent=owner.qualified_name,
            start_line=start,
            end_line=end,
            signature=signature,
            docstring=_docstring(body),
            decorators=decorators,
            code=code,
            content_hash=sha256_hex(code),
            bases=bases,
            is_async=any(c.type == "async" for c in node.children),
            nested=scope == "function",
        )
        self.units.append(unit)
        if is_class:
            # Base-class expressions and keyword args are evaluated in the enclosing scope.
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                self._collect_calls(supers, owner)
        else:
            params = node.child_by_field_name("parameters")
            if params is not None:
                self._collect_calls(params, unit)  # default values
                unit.local_types.update(self._annotated_params(params))
        if body is not None:
            self._visit_block(body, unit, scope="class" if is_class else "function")

    def _record_import(self, node: Node) -> None:
        line = node.start_point.row + 1
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    target = _text(child)
                    # `import a.b` binds `a`; the full path stays reachable via attribute access.
                    self.imports.append(
                        ImportBinding(target.split(".")[0], target.split(".")[0], line, True)
                    )
                    if "." in target:
                        self.imports.append(ImportBinding(target, target, line, True))
                elif child.type == "aliased_import":
                    target = _text(child.child_by_field_name("name"))
                    alias = _text(child.child_by_field_name("alias"))
                    self.imports.append(ImportBinding(alias, target, line, True))
            return
        # from X import a, b as c   /   from . import x   /   from .mod import *
        module_node = node.child_by_field_name("module_name")
        base = self._resolve_relative(_text(module_node)) if module_node is not None else ""
        for child in node.named_children:
            if child == module_node:
                continue
            if child.type == "dotted_name":
                name = _text(child)
                self.imports.append(ImportBinding(name, f"{base}.{name}".strip("."), line, False))
            elif child.type == "aliased_import":
                name = _text(child.child_by_field_name("name"))
                alias = _text(child.child_by_field_name("alias"))
                self.imports.append(ImportBinding(alias, f"{base}.{name}".strip("."), line, False))
            elif child.type == "wildcard_import":
                self.imports.append(ImportBinding("*", base, line, True))

    def _resolve_relative(self, spec: str) -> str:
        if not spec.startswith("."):
            return spec
        dots = len(spec) - len(spec.lstrip("."))
        rest = spec[dots:]
        package_parts = self.module.split(".")
        if not self.path.endswith("__init__.py"):
            package_parts = package_parts[:-1]  # a module's package is its parent
        if dots > 1:
            package_parts = package_parts[: len(package_parts) - (dots - 1)]
        return ".".join([*package_parts, rest] if rest else package_parts)

    @staticmethod
    def _is_main_guard(node: Node) -> bool:
        cond = node.child_by_field_name("condition")
        text = " ".join(_text(cond).split()) if cond is not None else ""
        return text in (
            '__name__ == "__main__"', "__name__ == '__main__'",
            '"__main__" == __name__', "'__main__' == __name__",
        )  # fmt: skip


def parse_python_file(path: str, text: str, *, decode_replaced: bool = False) -> ParsedFile:
    module = module_name_for(path)
    valid, error = is_valid_python3(text)
    if not valid:
        return ParsedFile(
            path=path,
            module=module,
            status="syntax_error",
            warning=f"Not valid Python 3 ({error}); indexed as plain text",
            units=[],
            imports=[],
        )
    source = text.encode("utf-8")
    tree = _parser().parse(source)
    extractor = _Extractor(path, module, source, split_lines(text))
    extractor.run(tree.root_node)
    return ParsedFile(
        path=path,
        module=module,
        status="decode_replaced" if decode_replaced else "ok",
        warning="Not valid UTF-8; decoded with replacement characters" if decode_replaced else None,
        units=extractor.units,
        imports=extractor.imports,
        main_block_line=extractor.main_block_line,
    )
