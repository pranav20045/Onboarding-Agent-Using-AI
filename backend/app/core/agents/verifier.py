"""Citation verifier: runs on every final answer, for every system.

Checks:
  1. every `[path:start-end]` citation points at an indexed file and a valid line range
  2. every code identifier in backticks exists in the symbol table or in the repo's text

It only checks existence, not whether a citation supports its claim (that is the judge's job).
If anything fails, the caller gets one revision pass with the list of problems.
"""

from __future__ import annotations

import builtins
import keyword
import re
from dataclasses import dataclass, field

from app.core.repo_context import PathError, RepoContext

CITATION_RE = re.compile(r"\[([A-Za-z0-9_./\-]+\.?[A-Za-z0-9_\-]*):(\d+)(?:-(\d+))?\]")
BACKTICK_RE = re.compile(r"(?<!`)`([^`\n]{1,120})`(?!`)")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")
_IGNORED = set(keyword.kwlist) | set(dir(builtins)) | {"self", "cls", "None", "True", "False"}


@dataclass
class Citation:
    path: str
    start: int
    end: int
    raw: str

    def as_dict(self) -> dict:
        return {"path": self.path, "start": self.start, "end": self.end, "raw": self.raw}


@dataclass
class VerificationResult:
    citations: list[Citation] = field(default_factory=list)
    invalid_citations: list[tuple[Citation, str]] = field(default_factory=list)
    identifiers_checked: list[str] = field(default_factory=list)
    hallucinated_symbols: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.invalid_citations and not self.hallucinated_symbols

    def problems_text(self) -> str:
        lines = []
        for c, why in self.invalid_citations:
            lines.append(f"- citation {c.raw}: {why}")
        for s in self.hallucinated_symbols:
            lines.append(
                f"- `{s}` does not exist in this repository (not in the symbol table or any file)"
            )
        return "\n".join(lines)


def parse_citations(text: str) -> list[Citation]:
    out = []
    for m in CITATION_RE.finditer(text):
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        out.append(Citation(m.group(1), start, end, m.group(0)))
    return out


def extract_identifiers(text: str) -> list[str]:
    """Code identifiers mentioned in single backticks (`name`, `mod.func`, `Class.method()`)."""
    found: list[str] = []
    for m in BACKTICK_RE.finditer(text):
        token = m.group(1).strip()
        if not IDENTIFIER_RE.match(token):
            continue  # code snippets, paths, expressions: not checked
        token = token.removesuffix("()")
        if token in _IGNORED or len(token) < 3:
            continue
        if token not in found:
            found.append(token)
    return found


def verify(answer: str, ctx: RepoContext) -> VerificationResult:
    result = VerificationResult(citations=parse_citations(answer))
    for c in result.citations:
        why = _check_citation(c, ctx)
        if why:
            result.invalid_citations.append((c, why))
    vocab = None
    for ident in extract_identifiers(answer):
        result.identifiers_checked.append(ident)
        if ctx.symbol_known(ident):
            continue
        vocab = vocab if vocab is not None else ctx.vocabulary
        parts = ident.split(".")
        # Dotted names: every segment must appear somewhere in the repo text
        # (covers attributes, stdlib calls used in the repo, config keys...).
        if all(p in vocab for p in parts):
            continue
        result.hallucinated_symbols.append(ident)
    return result


def _check_citation(c: Citation, ctx: RepoContext) -> str | None:
    if c.start < 1 or c.end < c.start:
        return "invalid line range"
    try:
        n = len(ctx.lines(c.path))
    except (PathError, FileNotFoundError) as exc:
        return f"file not found ({exc})"
    except OSError as exc:
        return f"file unreadable ({exc})"
    if c.end > n:
        return f"line range exceeds file length ({n} lines)"
    return None


def stats(before: VerificationResult, after: VerificationResult | None, revised: bool) -> dict:
    final = after or before
    return {
        "citations_total": len(final.citations),
        "invalid_before_revision": len(before.invalid_citations),
        "invalid_after_revision": len(final.invalid_citations),
        "hallucinated_symbols_before": len(before.hallucinated_symbols),
        "hallucinated_symbols_after": len(final.hallucinated_symbols),
        "hallucinated_symbols": final.hallucinated_symbols,
        "invalid_citations": [
            {"citation": c.raw, "reason": why} for c, why in final.invalid_citations
        ],
        "revised": revised,
        "ok": final.ok,
    }


REVISION_PROMPT = (
    "An automatic check of your answer found these problems:\n{problems}\n\n"
    "Rewrite the complete answer so that every citation points to an existing file and a valid "
    "line range, and remove or correct any identifier that does not exist in the repository. "
    "Do not add new claims you have not verified. Output only the revised answer."
)
