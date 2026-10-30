"""JSON (de)serialization of parse results, so incremental runs only re-parse changed files."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.core.models import CallSite, CodeUnit, ImportBinding, ParsedFile

CACHE_VERSION = 2


def parsed_to_dict(pf: ParsedFile) -> dict:
    return asdict(pf)


def parsed_from_dict(d: dict) -> ParsedFile:
    units = []
    for u in d["units"]:
        u = dict(u)
        u["calls"] = [CallSite(**c) for c in u.get("calls", [])]
        units.append(CodeUnit(**u))
    return ParsedFile(
        path=d["path"],
        module=d["module"],
        status=d["status"],
        warning=d.get("warning"),
        units=units,
        imports=[ImportBinding(**i) for i in d["imports"]],
        main_block_line=d.get("main_block_line"),
    )


def save_parse_cache(path: Path, entries: dict[str, tuple[str, ParsedFile]]) -> None:
    """entries: file path -> (content_hash, ParsedFile)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "files": {
            p: {"hash": h, "parsed": parsed_to_dict(pf)} for p, (h, pf) in sorted(entries.items())
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_parse_cache(path: Path) -> dict[str, tuple[str, ParsedFile]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("version") != CACHE_VERSION:
        return {}
    return {p: (e["hash"], parsed_from_dict(e["parsed"])) for p, e in payload["files"].items()}
