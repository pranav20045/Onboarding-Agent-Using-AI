"""Metrics, computed only from saved raw run files (so every number is reproducible).

Per question:
  recall@k (file)    |gold_files ∩ retrieved_files[:k]| / |gold_files|      (retrieval systems only)
  recall@k (symbol)  gold symbols found among retrieved units' qualified names (suffix match)
  gold_citation_hit  answer cites at least one gold file                      (all systems)
  score              judge score 0/1/2; correct = score == 2
  refusal            judge says the answer concludes "not found / does not exist"
  citation validity  from the verifier (after the revision pass)
  cost / tokens / latency / tool calls
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

RETRIEVAL_SYSTEMS = {"naive_rag", "indexed_agent"}
KS = (5, 10)


def load_run(run_dir: Path) -> tuple[dict, list[dict], dict[str, dict]]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    answers = [json.loads(ln) for ln in (run_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines() if ln]
    judgments: dict[str, dict] = {}
    jf = run_dir / "judgments.jsonl"
    if jf.exists():
        for ln in jf.read_text(encoding="utf-8").splitlines():
            if ln:
                j = json.loads(ln)
                judgments[j["id"]] = j
    return meta, answers, judgments


def _symbol_match(gold: str, retrieved: list[str]) -> bool:
    return any(r == gold or r.endswith("." + gold) for r in retrieved)


def per_question(answer: dict, question: dict, judgment: dict | None) -> dict:
    gold_files = question.get("gold_files") or []
    gold_syms = question.get("gold_symbols") or []
    system = answer["system"]
    row: dict = {"id": answer["id"], "repo": answer["repo"], "category": answer["category"], "system": system}
    for k in KS:
        if system in RETRIEVAL_SYSTEMS and gold_files:
            got = set(answer.get("retrieved_files", [])[:k])
            row[f"recall@{k}"] = len(got & set(gold_files)) / len(gold_files)
        else:
            row[f"recall@{k}"] = None
        if system in RETRIEVAL_SYSTEMS and gold_syms and answer.get("retrieved_symbols"):
            got_s = answer["retrieved_symbols"][:k]
            row[f"symbol_recall@{k}"] = sum(_symbol_match(s, got_s) for s in gold_syms) / len(gold_syms)
        else:
            row[f"symbol_recall@{k}"] = None
    cited = {c["path"] for c in answer.get("citations", [])}
    row["gold_citation_hit"] = (bool(cited & set(gold_files)) if gold_files else None)
    score = judgment.get("score") if judgment else None
    row["score"] = score
    row["correct"] = None if score is None else score == 2
    row["refused"] = judgment.get("refused") if judgment else None
    v = answer.get("verification") or {}
    row["citations"] = v.get("citations_total", 0)
    row["invalid_citations"] = v.get("invalid_after_revision", 0)
    row["invalid_citations_before"] = v.get("invalid_before_revision", 0)
    row["hallucinated_symbols"] = v.get("hallucinated_symbols_after", 0)
    u = answer.get("usage") or {}
    row.update(cost_usd=u.get("cost_usd"), tokens=u.get("total_tokens"), latency_s=u.get("latency_s"),
               tool_calls=u.get("tool_calls"), llm_calls=u.get("llm_calls"),
               budget_exhausted=answer.get("budget_exhausted", False), error=answer.get("error"))  # fmt: skip
    return row


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def percentile(xs: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default 'linear' method)."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * p / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def bootstrap_ci(xs: list[float], n: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def aggregate(rows: list[dict]) -> dict:
    scores = [r["score"] for r in rows]
    unans = [r for r in rows if r["category"] == "unanswerable"]
    answerable = [r for r in rows if r["category"] != "unanswerable"]
    ci = bootstrap_ci(scores)
    costs = [r["cost_usd"] for r in rows]
    return {
        "n": len(rows),
        "n_judged": sum(s is not None for s in scores),
        "mean_score": _mean(scores),
        "mean_score_ci95": ci,
        "pct_correct": _mean([None if r["correct"] is None else float(r["correct"]) for r in rows]),
        "pct_at_least_partial": _mean([None if s is None else float(s >= 1) for s in scores]),
        "refusal_rate_unanswerable": _mean([None if r["refused"] is None else float(r["refused"]) for r in unans]),
        "false_refusal_rate": _mean([None if r["refused"] is None else float(r["refused"]) for r in answerable]),
        **{f"recall@{k}": _mean([r[f"recall@{k}"] for r in rows]) for k in KS},
        **{f"symbol_recall@{k}": _mean([r[f"symbol_recall@{k}"] for r in rows]) for k in KS},
        "gold_citation_hit_rate": _mean([None if r["gold_citation_hit"] is None else float(r["gold_citation_hit"])
                                         for r in rows]),  # fmt: skip
        "invalid_citation_rate": (sum(r["invalid_citations"] for r in rows) / max(1, sum(r["citations"] for r in rows))),
        "answers_with_hallucinated_symbols": _mean([float(r["hallucinated_symbols"] > 0) for r in rows]),
        "mean_cost_usd": None if any(c is None for c in costs) else _mean(costs),
        "total_cost_usd": None if any(c is None for c in costs) else sum(costs),
        "mean_tokens": _mean([r["tokens"] for r in rows]),
        "latency_p50_s": percentile([r["latency_s"] for r in rows], 50),
        "latency_p95_s": percentile([r["latency_s"] for r in rows], 95),
        "mean_tool_calls": _mean([r["tool_calls"] for r in rows]),
        "budget_exhausted_rate": _mean([float(r["budget_exhausted"]) for r in rows]),
        "errors": sum(1 for r in rows if r["error"]),
    }  # fmt: skip


def breakdown(rows: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    return {k: aggregate(v) for k, v in sorted(groups.items())}
