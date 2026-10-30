"""Export a random sample of judged answers for blind human grading.

    uv run --project backend python -m eval.judge.export_calibration --n 30 [--runs RUN_ID ...]

Writes eval/results/calibration/to_grade.csv (no judge scores: grade the `human_score` column
0/1/2 by the same rubric) and calibration/key.json (judge scores, kept separate).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

from eval import REPO_ROOT
from eval.datasets.schema import load_dataset
from eval.judge.judge import RUBRIC, _dataset_path
from eval.metrics.compute import load_run

CAL = REPO_ROOT / "eval" / "results" / "calibration"
RAW = REPO_ROOT / "eval" / "results" / "raw"


def export(n: int, run_ids: list[str] | None, seed: int, out_dir: Path = CAL) -> int:
    pool = []
    dirs = [RAW / r for r in run_ids] if run_ids else sorted(p for p in RAW.glob("*") if (p / "meta.json").exists())
    for d in dirs:
        meta, answers, judgments = load_run(d)
        if meta.get("mock"):
            continue
        qs = {q.id: q for q in load_dataset(_dataset_path(meta["dataset"]), include_unverified=True)}
        for a in answers:
            j = judgments.get(a["id"])
            if j and j.get("score") is not None and a["id"] in qs:
                pool.append((meta["run_id"], a, qs[a["id"]], j))
    rng = random.Random(seed)
    sample = rng.sample(pool, min(n, len(pool)))
    out_dir.mkdir(parents=True, exist_ok=True)
    key = {}
    with (out_dir / "to_grade.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample_id", "category", "question", "reference_answer", "system_answer", "human_score", "notes"])
        for i, (run_id, a, q, j) in enumerate(sample, 1):
            sid = f"s{i:03d}"
            w.writerow([sid, q.category, q.question, q.reference_answer, a.get("answer", ""), "", ""])
            key[sid] = {"run_id": run_id, "id": a["id"], "system": a["system"], "judge_score": j["score"]}
    (out_dir / "key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    (out_dir / "RUBRIC.txt").write_text(RUBRIC, encoding="utf-8")
    return len(sample)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eval.judge.export_calibration")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--runs", nargs="*", default=None)
    p.add_argument("--seed", type=int, default=13)
    a = p.parse_args(argv)
    k = export(a.n, a.runs, a.seed)
    print(f"Exported {k} answers to {CAL / 'to_grade.csv'} (grade the human_score column 0/1/2)")


if __name__ == "__main__":
    main()
