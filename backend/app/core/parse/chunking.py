"""Turn parsed files into retrieval chunks.

AST chunking (the main system):
  * one chunk per top-level function and per method (nested functions stay inside their parent)
  * one "class summary" chunk per class: header, docstring and class-level statements, with
    method bodies collapsed to their signatures (methods have their own chunks)
  * module-level code outside any definition (imports, constants, `__main__` blocks) is chunked
    as contiguous segments
  * units larger than `max_chars` are split into overlapping line windows; every part carries
    the unit's qualified name and signature as a header

Text chunking: Markdown by headings, other text by blank-line paragraphs.
Fixed chunking: line-snapped windows of ~`size_chars` with overlap, for the naive baseline and
the chunking ablation. Every chunk keeps exact 1-based line ranges so citations stay valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from app.core.models import (
    Chunk,
    CodeUnit,
    ParsedFile,
    SourceFile,
    sha256_hex,
    split_lines,
    stable_id,
)


@dataclass(frozen=True)
class ChunkConfig:
    max_chars: int = 6000  # ~1500 tokens at ~4 chars/token
    overlap_lines: int = 8
    fixed_size_chars: int = 2000
    fixed_overlap_chars: int = 200


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _chunk(
    path: str, start: int, end: int, kind: str, body: str, header: str,
    qname: str | None = None, unit_kind: str | None = None, part: int = 0,
) -> Chunk:  # fmt: skip
    text = f"{header}\n{body}" if header else body
    return Chunk(
        id=stable_id(path, kind, qname or "", str(start), str(end), str(part)),
        path=path,
        start_line=start,
        end_line=end,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        content_hash=sha256_hex(text),
        qualified_name=qname,
        unit_kind=unit_kind,
        part=part,
    )


def _windows(
    lines: list[str], first_line: int, max_chars: int, overlap: int
) -> list[tuple[int, int]]:
    """Line windows (1-based inclusive) whose text is at most ~max_chars each."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        size = 0
        j = i
        while j < n and (size + len(lines[j]) + 1 <= max_chars or j == i):
            size += len(lines[j]) + 1
            j += 1
        spans.append((first_line + i, first_line + j - 1))
        if j >= n:
            break
        i = max(j - overlap, i + 1)
    return spans


def _split_unit(
    path: str, start: int, end: int, lines: list[str], header: str, signature: str,
    qname: str, unit_kind: str, cfg: ChunkConfig,
) -> list[Chunk]:  # fmt: skip
    body = "\n".join(lines)
    if len(body) <= cfg.max_chars:
        return [_chunk(path, start, end, "code", body, header, qname, unit_kind)]
    spans = _windows(lines, start, cfg.max_chars, cfg.overlap_lines)
    chunks = []
    for part, (s, e) in enumerate(spans):
        part_header = f"{header}\n# part {part + 1}/{len(spans)} of: {signature}"
        part_body = "\n".join(lines[s - start : e - start + 1])
        chunks.append(_chunk(path, s, e, "code", part_body, part_header, qname, unit_kind, part))
    return chunks


def _header(
    path: str, start: int, end: int, unit: CodeUnit | None, label: str | None = None
) -> str:
    if unit is None:
        return f"# {path}:{start}-{end} {label or ''}".rstrip()
    return f"# {path}:{start}-{end} {unit.kind} {unit.qualified_name}"


def ast_chunks(
    parsed: ParsedFile, source: SourceFile, cfg: ChunkConfig | None = None
) -> list[Chunk]:
    cfg = cfg or ChunkConfig()
    if parsed.status == "syntax_error" or not parsed.units:
        return text_fallback_chunks(source, cfg)
    lines = split_lines(source.text)
    by_qname = {u.qualified_name: u for u in parsed.units}

    def inside_function(u: CodeUnit) -> bool:
        parent = by_qname.get(u.parent or "")
        while parent is not None:
            if parent.kind in ("function", "method"):
                return True
            parent = by_qname.get(parent.parent or "")
        return False

    chunks: list[Chunk] = []
    covered = [False] * (len(lines) + 2)
    for u in parsed.units:
        if u.kind == "module" or inside_function(u):
            continue
        if u.parent == parsed.module:  # top-level definition: exclude from module remainder
            for ln in range(u.start_line, u.end_line + 1):
                covered[ln] = True
        if u.kind in ("function", "method"):
            body_lines = lines[u.start_line - 1 : u.end_line]
            chunks += _split_unit(
                u.path, u.start_line, u.end_line, body_lines,
                _header(u.path, u.start_line, u.end_line, u), u.signature,
                u.qualified_name, u.kind, cfg,
            )  # fmt: skip
        elif u.kind == "class":
            chunks += _class_summary(u, parsed, lines, cfg)

    # Module-level code outside definitions, as contiguous segments.
    segment: list[int] = []
    for ln in range(1, len(lines) + 2):
        if ln <= len(lines) and not covered[ln]:
            segment.append(ln)
            continue
        if segment and any(lines[i - 1].strip() for i in segment):
            s, e = segment[0], segment[-1]
            while not lines[s - 1].strip():
                s += 1
            while not lines[e - 1].strip():
                e -= 1
            seg_lines = lines[s - 1 : e]
            module_unit = by_qname.get(parsed.module)
            chunks += _split_unit(
                parsed.path, s, e, seg_lines,
                _header(parsed.path, s, e, None, f"module {parsed.module} (module-level code)"),
                f"module {parsed.module}", parsed.module, "module", cfg,
            ) if module_unit else []  # fmt: skip
        segment = []
    return sorted(chunks, key=lambda c: (c.start_line, c.end_line, c.part))


def _class_summary(
    cls: CodeUnit, parsed: ParsedFile, lines: list[str], cfg: ChunkConfig
) -> list[Chunk]:
    """Class source with direct methods collapsed to `<signature>: ...` (they have own chunks)."""
    children = sorted(
        (
            u
            for u in parsed.units
            if u.parent == cls.qualified_name and u.kind in ("method", "class")
        ),
        key=lambda u: u.start_line,
    )
    out: list[str] = []
    ln = cls.start_line
    for child in children:
        out.extend(lines[ln - 1 : child.start_line - 1])
        indent = lines[child.start_line - 1][
            : len(lines[child.start_line - 1]) - len(lines[child.start_line - 1].lstrip())
        ]
        for dec in child.decorators:
            out.append(f"{indent}@{dec}")
        out.append(f"{indent}{child.signature}: ...")
        ln = child.end_line + 1
    out.extend(lines[ln - 1 : cls.end_line])
    body = "\n".join(out)
    if len(body) > cfg.max_chars:
        body = body[: cfg.max_chars] + "\n# ... (class summary truncated)"
    header = _header(cls.path, cls.start_line, cls.end_line, cls) + " (summary; methods collapsed)"
    return [_chunk(cls.path, cls.start_line, cls.end_line, "code", body, header,
                   cls.qualified_name, "class")]  # fmt: skip


def text_fallback_chunks(source: SourceFile, cfg: ChunkConfig | None = None) -> list[Chunk]:
    """Python files that do not parse: plain line windows, flagged as such in the header."""
    cfg = cfg or ChunkConfig()
    lines = split_lines(source.text)
    chunks = []
    for s, e in _windows(lines, 1, cfg.max_chars, cfg.overlap_lines):
        body = "\n".join(lines[s - 1 : e])
        if body.strip():
            header = f"# {source.path}:{s}-{e} (unparsed Python, plain text)"
            chunks.append(_chunk(source.path, s, e, "text_fallback", body, header))
    return chunks


def doc_chunks(source: SourceFile, cfg: ChunkConfig | None = None) -> list[Chunk]:
    """Markdown/RST split at headings; other text at blank lines. Sections are size-capped."""
    cfg = cfg or ChunkConfig()
    lines = split_lines(source.text)
    is_md = source.path.lower().endswith((".md", ".markdown", ".rst")) or "README" in source.path
    boundaries: list[int] = [1]
    in_fence = False
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if i == 1 or in_fence:
            continue
        if is_md and line.startswith("#"):
            boundaries.append(i)
        elif not is_md and not line.strip() and i < len(lines) and lines[i].strip():
            boundaries.append(i + 1)
    boundaries = sorted(set(boundaries)) + [len(lines) + 1]

    # Merge small neighbouring sections up to max_chars, then cap oversized ones.
    sections: list[tuple[int, int]] = []
    for s, nxt in pairwise(boundaries):
        e = nxt - 1
        if sections:
            ps, _pe = sections[-1]
            if sum(len(x) + 1 for x in lines[ps - 1 : e]) <= cfg.max_chars // 3:
                sections[-1] = (ps, e)
                continue
        sections.append((s, e))

    chunks: list[Chunk] = []
    for s, e in sections:
        for ws, we in _windows(lines[s - 1 : e], s, cfg.max_chars, cfg.overlap_lines):
            body = "\n".join(lines[ws - 1 : we])
            if body.strip():
                header = f"# {source.path}:{ws}-{we} (document)"
                chunks.append(_chunk(source.path, ws, we, "doc", body, header))
    return chunks


def fixed_chunks(source: SourceFile, cfg: ChunkConfig | None = None) -> list[Chunk]:
    """Naive baseline: ~fixed_size_chars windows snapped to whole lines, with char overlap."""
    cfg = cfg or ChunkConfig()
    lines = split_lines(source.text)
    overlap_lines = max(1, cfg.fixed_overlap_chars // 60)  # ~60 chars per line on average
    chunks = []
    for s, e in _windows(lines, 1, cfg.fixed_size_chars, overlap_lines):
        body = "\n".join(lines[s - 1 : e])
        if body.strip():
            chunks.append(_chunk(source.path, s, e, "fixed", body, f"# {source.path}:{s}-{e}"))
    return chunks


def chunks_for_file(
    source: SourceFile,
    parsed: ParsedFile | None,
    strategy: str = "ast",
    cfg: ChunkConfig | None = None,
) -> list[Chunk]:
    """Dispatch: `strategy` is "ast" (structural for Python, sections for docs) or "fixed"."""
    if strategy == "fixed":
        return fixed_chunks(source, cfg)
    if source.language == "python" and parsed is not None:
        return ast_chunks(parsed, source, cfg)
    if source.language == "python":
        return text_fallback_chunks(source, cfg)
    return doc_chunks(source, cfg)
