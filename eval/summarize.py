"""Build eval/results/summary.{json,md} and charts from saved raw runs ONLY.

    uv run --project backend python -m eval.summarize [--runs RUN_ID ...] [--include-mock]

Every number in the summary is recomputed from eval/results/raw/<run_id>/ files, so the README
(which copies summary.md) always traces back to saved runs and configs.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from eval import REPO_ROOT
from eval.datasets.schema import load_dataset
from eval.judge.judge import _dataset_path
from eval.metrics.compute import aggregate, breakdown, load_run, per_question

RESULTS = REPO_ROOT / "eval" / "results"
RAW = RESULTS / "raw"
ABLATION_ORDER = ["naive_rag", "ablation_1_ast", "ablation_2_hybrid", "ablation_3_rerank", "ablation_4_graph", "full"]
MAIN_ORDER = ["naive_rag", "full", "grep_agent", "grep_agent_github", "full_ollama"]
FAIRNESS_KEYS = ("llm_model", "max_tool_calls", "max_tokens", "tool_output_max_chars", "answer_max_tokens")


def label_for(meta: dict) -> str:
    return Path(meta.get("config_file") or meta["config"]["system"]).stem


def collect(run_ids: list[str] | None, include_mock: bool, raw: Path = RAW) -> list[tuple[dict, list[dict]]]:
    runs = []
    dirs = [raw / r for r in run_ids] if run_ids else sorted(p for p in raw.glob("*") if (p / "meta.json").exists())
    for d in dirs:
        meta, answers, judgments = load_run(d)
        if meta.get("mock") and not include_mock:
            continue
        qs = {q.id: q.model_dump() for q in load_dataset(_dataset_path(meta["dataset"]), include_unverified=True)}
        rows = [per_question(a, qs[a["id"]], judgments.get(a["id"])) for a in answers if a["id"] in qs]
        runs.append((meta, rows))
    return runs


def fairness_warnings(by_label: dict[str, list[tuple[dict, list[dict]]]]) -> list[str]:
    warns = []
    cfgs = {label: [m["config"] for m, _ in runs] for label, runs in by_label.items()}
    for key in FAIRNESS_KEYS:
        values = {label: {c.get(key) for c in cs} for label, cs in cfgs.items() if label != "full_ollama"}
        flat = set().union(*values.values()) if values else set()
        if len(flat) > 1:
            warns.append(f"Compared runs differ in {key}: {values}")
    qsets = {label: sorted({qid for m, _ in runs for qid in m["question_ids"]}) for label, runs in by_label.items()}
    if len({tuple(v) for v in qsets.values()}) > 1:
        warns.append("Compared runs used different question sets: " + ", ".join(f"{k}={len(v)}" for k, v in qsets.items()))
    return warns


def _fmt(v, pct: bool = False, money: bool = False, digits: int = 2) -> str:
    if v is None:
        return "–"
    if pct:
        return f"{100 * v:.0f}%"
    if money:
        return f"${v:.4f}"
    return f"{v:.{digits}f}"


def _score(a: dict) -> str:
    if a["mean_score"] is None:
        return "–"
    ci = a["mean_score_ci95"]
    return f"{a['mean_score']:.2f}" + (f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "")


def table(labels: list[str], aggs: dict[str, dict]) -> str:
    head = ("| System | n | Mean score (0-2) [95% CI] | Correct | Refusal on unanswerable | Recall@5 | "
            "Gold-file cited | Invalid citations | Mean cost | p50 / p95 latency | Tool calls |")  # fmt: skip
    lines = [head, "|" + "---|" * 11]
    for label in labels:
        a = aggs.get(label)
        if not a:
            continue
        lines.append(
            f"| {label} | {a['n']} | {_score(a)} | {_fmt(a['pct_correct'], pct=True)} | "
            f"{_fmt(a['refusal_rate_unanswerable'], pct=True)} | {_fmt(a['recall@5'], pct=True)} | "
            f"{_fmt(a['gold_citation_hit_rate'], pct=True)} | {_fmt(a['invalid_citation_rate'], pct=True)} | "
            f"{_fmt(a['mean_cost_usd'], money=True)} | {_fmt(a['latency_p50_s'], digits=1)}s / "
            f"{_fmt(a['latency_p95_s'], digits=1)}s | {_fmt(a['mean_tool_calls'], digits=1)} |"
        )
    return "\n".join(lines)


def category_table(labels: list[str], per_cat: dict[str, dict[str, dict]]) -> str:
    cats = ["locate", "explain", "flow", "why", "unanswerable"]
    lines = ["| System | " + " | ".join(cats) + " |", "|" + "---|" * (len(cats) + 1)]
    for label in labels:
        if label not in per_cat:
            continue
        cells = []
        for c in cats:
            a = per_cat[label].get(c)
            cells.append("–" if not a or a["mean_score"] is None else f"{a['mean_score']:.2f} (n={a['n']})")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def charts(aggs: dict[str, dict], per_cat: dict[str, dict[str, dict]], out: Path) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    made = []
    pts = [(lbl, a["mean_cost_usd"], a["mean_score"]) for lbl, a in aggs.items()
           if a["mean_cost_usd"] is not None and a["mean_score"] is not None]  # fmt: skip
    if pts:
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for lbl, x, y in pts:
            ax.scatter(x, y, s=60)
            ax.annotate(lbl, (x, y), textcoords="offset points", xytext=(6, 4), fontsize=9)
        ax.set_xlabel("Mean cost per question (USD)")
        ax.set_ylabel("Mean judge score (0-2)")
        ax.set_title("Correctness vs cost")
        ax.set_ylim(0, 2)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(out / "correctness_vs_cost.png", dpi=150)
        plt.close(fig)
        made.append("correctness_vs_cost.png")
    cats = ["locate", "explain", "flow", "why", "unanswerable"]
    labels = list(per_cat)
    if labels:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        width = 0.8 / len(labels)
        for i, lbl in enumerate(labels):
            ys = [(per_cat[lbl].get(c) or {}).get("mean_score") or 0 for c in cats]
            ax.bar([j + i * width for j in range(len(cats))], ys, width, label=lbl)
        ax.set_xticks([j + 0.4 - width / 2 for j in range(len(cats))], cats)
        ax.set_ylabel("Mean judge score (0-2)")
        ax.set_ylim(0, 2)
        ax.set_title("Score by question category")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(out / "score_by_category.png", dpi=150)
        plt.close(fig)
        made.append("score_by_category.png")
    return made


def build(run_ids: list[str] | None = None, include_mock: bool = False, out: Path = RESULTS,
          raw: Path = RAW) -> dict:  # fmt: skip
    runs = collect(run_ids, include_mock, raw)
    by_label: dict[str, list[tuple[dict, list[dict]]]] = defaultdict(list)
    for meta, rows in runs:
        by_label[label_for(meta)].append((meta, rows))
    rows_by_label = {lbl: [r for _, rows in rs for r in rows] for lbl, rs in by_label.items()}
    aggs = {lbl: aggregate(rows) for lbl, rows in rows_by_label.items()}
    per_cat = {lbl: breakdown(rows, "category") for lbl, rows in rows_by_label.items()}
    per_repo = {lbl: breakdown(rows, "repo") for lbl, rows in rows_by_label.items()}
    summary = {
        "runs": [{"run_id": m["run_id"], "label": label_for(m), "model": m["model"], "judge_model": m["judge_model"],
                  "codeatlas_version": m["codeatlas_version"], "dataset": m["dataset"], "n": len(rows),
                  "mock": m.get("mock", False), "created_at": m["created_at"]} for m, rows in runs],
        "aggregate": aggs, "by_category": per_cat, "by_repo": per_repo,
        "fairness_warnings": fairness_warnings(by_label),
    }  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    summary["charts"] = charts(aggs, per_cat, out) if aggs else []
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    (out / "summary.md").write_text(render_md(summary), encoding="utf-8")
    return summary


def render_md(s: dict) -> str:
    if not s["runs"]:
        return "# Evaluation summary\n\nNo evaluation runs have been recorded yet.\n"
    aggs, per_cat = s["aggregate"], s["by_category"]
    labels = list(aggs)
    main = [lbl for lbl in MAIN_ORDER if lbl in aggs] + [lbl for lbl in labels if lbl not in MAIN_ORDER + ABLATION_ORDER]
    abl = [lbl for lbl in ABLATION_ORDER if lbl in aggs]
    parts = ["# Evaluation summary", "",
             "Generated by `python -m eval.summarize` from the raw runs listed at the bottom. "
             "Scores are LLM-judge scores (0 wrong, 1 partial, 2 correct); brackets are bootstrap 95% CIs.", ""]  # fmt: skip
    if any(r["mock"] for r in s["runs"]):
        parts += ["> **WARNING: includes MOCK runs (offline pipeline check). These numbers are meaningless.**", ""]
    if s["fairness_warnings"]:
        parts += ["## Fairness warnings", "", *[f"- {w}" for w in s["fairness_warnings"]], ""]
    parts += ["## Main comparison", "", table(main, aggs), ""]
    if len(abl) > 1:
        parts += ["## Ablation (adding one component at a time)", "", table(abl, aggs), ""]
    parts += ["## Mean score by category", "", category_table(main + [a for a in abl if a not in main], per_cat), ""]
    for c in s.get("charts", []):
        parts += [f"![{c}]({c})", ""]
    parts += ["## Runs", "", "| Run | Label | Model | Judge | n | CodeAtlas version |", "|---|---|---|---|---|---|"]
    parts += [f"| {r['run_id']} | {r['label']} | {r['model']} | {r['judge_model']} | {r['n']} | `{r['codeatlas_version']}` |"
              for r in s["runs"]]  # fmt: skip
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eval.summarize")
    p.add_argument("--runs", nargs="*", default=None)
    p.add_argument("--include-mock", action="store_true")
    p.add_argument("--out", default=str(RESULTS))
    a = p.parse_args(argv)
    s = build(a.runs, a.include_mock, Path(a.out))
    print(f"Summarized {len(s['runs'])} runs -> {Path(a.out) / 'summary.md'}")


if __name__ == "__main__":
    main()
