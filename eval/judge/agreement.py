"""Agreement between human grades and the LLM judge (exact agreement + Cohen's kappa).

    uv run --project backend python -m eval.judge.agreement

Reads calibration/to_grade.csv (with human_score filled in) and calibration/key.json, writes
eval/results/calibration/agreement.json. Reports unweighted and quadratic-weighted kappa.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from eval.judge.export_calibration import CAL

LABELS = (0, 1, 2)


def cohen_kappa(a: list[int], b: list[int], weighted: bool = False) -> float | None:
    n = len(a)
    if n == 0:
        return None
    k = len(LABELS)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b, strict=True):
        obs[LABELS.index(x)][LABELS.index(y)] += 1
    ra = [sum(obs[i]) for i in range(k)]
    cb = [sum(obs[i][j] for i in range(k)) for j in range(k)]

    def w(i: int, j: int) -> float:
        return ((i - j) ** 2) / ((k - 1) ** 2) if weighted else float(i != j)

    observed = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k)) / n
    expected = sum(w(i, j) * ra[i] * cb[j] for i in range(k) for j in range(k)) / (n * n)
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1 - observed / expected


def compute(cal_dir: Path = CAL) -> dict:
    key = json.loads((cal_dir / "key.json").read_text(encoding="utf-8"))
    human, judge = [], []
    with (cal_dir / "to_grade.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs = (row.get("human_score") or "").strip()
            if hs == "":
                continue
            if hs not in ("0", "1", "2"):
                raise ValueError(f"{row['sample_id']}: human_score must be 0, 1 or 2")
            human.append(int(hs))
            judge.append(int(key[row["sample_id"]]["judge_score"]))
    n = len(human)
    confusion = {f"human{h}_judge{j}": sum(1 for x, y in zip(human, judge, strict=True) if (x, y) == (h, j))
                 for h in LABELS for j in LABELS}  # fmt: skip
    result = {
        "n_graded": n,
        "n_exported": len(key),
        "exact_agreement": (sum(x == y for x, y in zip(human, judge, strict=True)) / n) if n else None,
        "cohen_kappa": cohen_kappa(human, judge),
        "cohen_kappa_quadratic": cohen_kappa(human, judge, weighted=True),
        "mean_human": (sum(human) / n) if n else None,
        "mean_judge": (sum(judge) / n) if n else None,
        "confusion": confusion,
    }
    (cal_dir / "agreement.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(compute(), indent=1))
