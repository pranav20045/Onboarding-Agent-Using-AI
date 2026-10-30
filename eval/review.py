"""Interactive review of drafted (unverified) eval questions. Only a human can verify.

    uv run --project backend python -m eval.review eval/datasets/flask.jsonl

For each unverified question it shows the question, reference answer and the gold files'
relevant lines at the pinned commit, then asks: [a]ccept  [e]dit  [r]eject  [s]kip  [q]uit.
Progress is saved after every action. Rejected questions move to <dataset>.rejected.jsonl.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from eval import REPO_ROOT
from eval.datasets.schema import Question, load_dataset, save_dataset

from app.config import get_settings  # noqa: E402
from app.db.store import repo_id_for_slug  # noqa: E402

LINE_REF = re.compile(r"([\w./-]+\.\w+):(\d+)-(\d+)")


def _show(checkout: Path, sha: str, path: str) -> list[str]:
    out = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=checkout, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")  # fmt: skip
    return out.stdout.split("\n") if out.returncode == 0 else []


def excerpts(q: Question, checkout: Path, max_lines: int = 25) -> str:
    blocks = []
    refs = LINE_REF.findall(f"{q.reference_answer}\n{q.notes or ''}")
    shown = set()
    for path, s, e in refs:
        if path not in q.gold_files or (path, s, e) in shown:
            continue
        shown.add((path, s, e))
        lines = _show(checkout, q.commit_sha, path)
        s_i, e_i = int(s), min(int(e), int(s) + max_lines - 1)
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(s_i, min(e_i, len(lines)) + 1))
        blocks.append(f"--- {path}:{s}-{e}\n{body}")
    for path in q.gold_files:
        if any(p == path for p, _, _ in shown):
            continue
        lines = _show(checkout, q.commit_sha, path)
        hits = [i for i, ln in enumerate(lines, 1) for sym in q.gold_symbols
                if re.search(rf"(def|class)\s+{re.escape(sym.split('.')[-1])}\b", ln)]  # fmt: skip
        start = hits[0] if hits else 1
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, min(start + max_lines, len(lines) + 1)))
        blocks.append(f"--- {path} (from line {start}; {len(lines)} lines)\n{body}")
    return "\n".join(blocks) or "(no gold files)"


def edit(q: Question) -> Question:
    editable = {k: getattr(q, k) for k in ("question", "reference_answer", "gold_files", "gold_symbols", "category", "notes")}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(editable, tf, indent=2)
        name = tf.name
    editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "nano")
    subprocess.run([editor, name], check=False)
    data = json.loads(Path(name).read_text(encoding="utf-8"))
    Path(name).unlink(missing_ok=True)
    return q.model_copy(update={**data, "source": "manual" if data != editable else q.source})


def review(path: Path) -> None:
    settings = get_settings()
    questions = load_dataset(path, include_unverified=True)
    rejected_path = path.with_suffix(".rejected.jsonl")
    todo = [q for q in questions if not q.verified]
    print(f"{len(todo)} unverified of {len(questions)} questions in {path}")
    for q in todo:
        checkout = settings.repos_dir / repo_id_for_slug(q.repo) / "checkout"
        print("\n" + "=" * 100)
        print(f"{q.id}  [{q.category}]  source={q.source}")
        print(f"Q: {q.question}\n\nReference:\n{q.reference_answer}\n")
        print(f"Gold files: {q.gold_files}  Gold symbols: {q.gold_symbols}")
        if q.notes:
            print(f"Notes: {q.notes}")
        print(excerpts(q, checkout))
        while True:
            choice = input("\n[a]ccept  [e]dit  [r]eject  [s]kip  [q]uit > ").strip().lower()
            if choice in ("a", "e", "r", "s", "q"):
                break
        if choice == "q":
            break
        if choice == "s":
            continue
        idx = next(i for i, x in enumerate(questions) if x.id == q.id)
        if choice == "r":
            with rejected_path.open("a", encoding="utf-8") as f:
                f.write(questions.pop(idx).model_dump_json() + "\n")
        else:
            updated = edit(q) if choice == "e" else q
            questions[idx] = updated.model_copy(update={"verified": True})  # set only by a human keypress
        save_dataset(path, questions)
    print(f"Saved. Verified: {sum(q.verified for q in questions)} / {len(questions)}")


if __name__ == "__main__":
    review(Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "eval" / "datasets" / "flask.jsonl")
