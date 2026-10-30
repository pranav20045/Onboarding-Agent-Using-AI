"""Question schema, loading and validation for eval datasets (JSONL, one question per line)."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["locate", "explain", "flow", "why", "unanswerable"]
CATEGORIES: tuple[str, ...] = ("locate", "explain", "flow", "why", "unanswerable")
SHA_IN_TEXT = re.compile(r"\b[0-9a-f]{7,40}\b")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z]+-\d{3}$")
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    category: Category
    question: str = Field(min_length=10)
    reference_answer: str = Field(min_length=10)
    gold_files: list[str] = Field(default_factory=list)
    gold_symbols: list[str] = Field(default_factory=list)
    source: Literal["llm_draft", "github_issue", "manual"]
    verified: bool = False
    notes: str | None = None


def load_dataset(path: Path, include_unverified: bool = False) -> list[Question]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            q = Question.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{n}: {exc}") from exc
        if q.verified or include_unverified:
            out.append(q)
    return out


def save_dataset(path: Path, questions: list[Question]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(q.model_dump_json() + "\n" for q in questions), encoding="utf-8")
    tmp.replace(path)


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=checkout, capture_output=True, text=True, check=False)


def validate(path: Path, checkout: Path) -> list[str]:
    """Return a list of problems (empty = valid). Never modifies the dataset."""
    problems: list[str] = []
    try:
        qs = load_dataset(path, include_unverified=True)
    except ValueError as exc:
        return [str(exc)]
    ids = Counter(q.id for q in qs)
    problems += [f"duplicate id {i}" for i, n in ids.items() if n > 1]
    shas = {q.commit_sha for q in qs}
    if len(shas) > 1:
        problems.append(f"more than one commit_sha in dataset: {sorted(shas)}")
    for q in qs:
        where = f"{q.id}:"
        if _git(checkout, "cat-file", "-e", f"{q.commit_sha}^{{commit}}").returncode != 0:
            problems.append(f"{where} commit {q.commit_sha} not in checkout")
            continue
        for f in q.gold_files:
            if _git(checkout, "cat-file", "-e", f"{q.commit_sha}:{f}").returncode != 0:
                problems.append(f"{where} gold file {f} does not exist at the pinned commit")
        for sym in q.gold_symbols:
            name = sym.split(".")[-1]
            found = any(
                re.search(rf"^\s*(async\s+def|def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*(:[^=\n]+)?=",
                          _git(checkout, "show", f"{q.commit_sha}:{f}").stdout, re.M)
                for f in q.gold_files
            )  # fmt: skip
            if not found:
                problems.append(f"{where} gold symbol {sym} not defined in its gold files")
        if q.category == "unanswerable" and q.gold_files:
            problems.append(f"{where} unanswerable questions should have no gold files")
        if q.category != "unanswerable" and not q.gold_files:
            problems.append(f"{where} answerable questions need gold files")
        if q.category == "why":
            cited = [s for s in SHA_IN_TEXT.findall(q.reference_answer)
                     if _git(checkout, "cat-file", "-e", f"{s}^{{commit}}").returncode == 0]  # fmt: skip
            if not cited:
                problems.append(f"{where} 'why' reference answer must cite a real commit sha")
    return problems


def summary(path: Path) -> dict:
    qs = load_dataset(path, include_unverified=True)
    return {
        "total": len(qs),
        "verified": sum(q.verified for q in qs),
        "by_category": dict(Counter(q.category for q in qs)),
        "verified_by_category": dict(Counter(q.category for q in qs if q.verified)),
    }


if __name__ == "__main__":
    import sys

    ds, co = Path(sys.argv[1]), Path(sys.argv[2])
    errs = validate(ds, co)
    print(json.dumps(summary(ds), indent=1))
    print("\n".join(errs) or "OK: no problems found")
    raise SystemExit(1 if errs else 0)
