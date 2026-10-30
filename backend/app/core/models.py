"""Plain data types shared across the core pipeline (ingest -> parse -> graph -> index)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Literal

UnitKind = Literal["module", "class", "function", "method"]
ChunkKind = Literal["code", "doc", "fixed", "text_fallback"]
ParseStatus = Literal["ok", "syntax_error", "decode_replaced", "text_only"]

_ID_NAMESPACE = uuid.UUID("6b1c3a52-5d0e-4c2c-9a57-0c0de0a71a50")
_LF = chr(10)
_CR = chr(13)


def stable_id(*parts: str) -> str:
    """Deterministic UUID string from parts. Same input -> same id on every (re)index."""
    return str(uuid.uuid5(_ID_NAMESPACE, chr(31).join(parts)))


def split_lines(text: str) -> list[str]:
    """Split on LF only (like git and tree-sitter), dropping a trailing CR per line.

    `str.splitlines()` also breaks on form feeds, U+2028 etc., which would shift line numbers
    relative to tree-sitter rows and `git blame`.
    """
    lines = text.split(_LF)
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith(_CR) else ln for ln in lines]


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class SourceFile:
    path: str  # repo-relative, POSIX separators
    language: Literal["python", "text"]
    size_bytes: int
    content_hash: str  # sha256 of the raw bytes
    text: str
    decode_replaced: bool = False

    @property
    def lines(self) -> list[str]:
        return split_lines(self.text)


@dataclass
class CallSite:
    raw: str  # callee expression as written, e.g. "self.validate", "with_retry", "os.path.join"
    line: int


@dataclass
class ImportBinding:
    """A name bound by an import statement: `alias` refers to `target` (a dotted path)."""

    alias: str
    target: str
    line: int
    is_module: bool  # `import x.y` (module) vs `from x import y` (attribute, maybe a module)


@dataclass
class CodeUnit:
    id: str
    path: str
    kind: UnitKind
    name: str
    qualified_name: str
    parent: str | None
    start_line: int
    end_line: int
    signature: str
    docstring: str | None
    decorators: list[str]
    code: str
    content_hash: str
    bases: list[str] = field(default_factory=list)  # classes only, raw base expressions
    calls: list[CallSite] = field(default_factory=list)  # calls made directly in this unit
    is_async: bool = False
    nested: bool = False  # defined inside a function
    # Best-effort local types: variable -> raw class expression, from `x = Cls(...)` and
    # annotated parameters `def f(x: Cls)`. Used only for call resolution.
    local_types: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedFile:
    path: str
    module: str  # dotted module name, e.g. "shop.services"
    status: ParseStatus
    warning: str | None
    units: list[CodeUnit]
    imports: list[ImportBinding]
    main_block_line: int | None = None  # line of `if __name__ == "__main__":`


@dataclass
class Chunk:
    id: str
    path: str
    start_line: int
    end_line: int
    kind: ChunkKind
    text: str  # what gets embedded / shown (includes a context header)
    content_hash: str
    qualified_name: str | None = None
    unit_kind: str | None = None
    part: int = 0  # sub-chunk index for split units
