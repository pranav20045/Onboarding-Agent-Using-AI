"""Run one system over a dataset and save raw outputs (+ judge scores).

    uv run --project backend python -m eval.run --dataset eval/datasets/flask.jsonl \
        --config configs/full.yaml [--system grep_agent] [--limit N] [--include-unverified] [--yes]

Saves eval/results/raw/<run_id>/{meta.json, answers.jsonl, judgments.jsonl}. Only `verified`
questions are used unless --include-unverified. The system receives only the question text:
reference answers and gold files never leave this process.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from eval import REPO_ROOT

from app.config import get_settings  # noqa: E402  (path set up by eval/__init__)
from app.core.llm.base import PRICES_PER_MTOK  # noqa: E402
from app.core.services import build_services  # noqa: E402
from app.core.systems.config import SystemConfig  # noqa: E402
from app.core.systems.registry import get_system  # noqa: E402
from app.db import store  # noqa: E402
from eval.datasets.schema import Question, load_dataset  # noqa: E402

RESULTS = REPO_ROOT / "eval" / "results"
RAW = RESULTS / "raw"
COST_GUARD_USD = 2.0
# Rough per-question token profiles used only for the pre-run cost estimate (deliberately
# pessimistic: agent context is re-sent each step; cache discounts are ignored).
EST_TOKENS = {"naive_rag": (9_000, 900), "indexed_agent": (90_000, 3_000), "grep_agent": (90_000, 3_000)}
EST_JUDGE_TOKENS = (3_000, 250)


def codeatlas_version() -> str:
    """git SHA of CodeAtlas if this is a git checkout, else a hash of the backend sources."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
        return out.stdout.strip() + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        h = hashlib.sha256()
        for p in sorted((REPO_ROOT / "backend" / "app").rglob("*.py")):
            h.update(p.relative_to(REPO_ROOT).as_posix().encode())
            h.update(p.read_bytes())
        return "nogit-src-" + h.hexdigest()[:16]


def estimate_cost(system: str, model: str, judge_model: str, n: int) -> float | None:
    p = PRICES_PER_MTOK.get(model)
    jp = PRICES_PER_MTOK.get(judge_model)
    if p is None or jp is None:
        return None
    tin, tout = EST_TOKENS[system]
    jin, jout = EST_JUDGE_TOKENS
    return n * ((tin * p[0] + tout * p[1]) + (jin * jp[0] + jout * jp[1])) / 1_000_000


async def run(args: argparse.Namespace, svc=None, raw_dir: Path = RAW) -> Path:
    settings = svc.settings if svc is not None else get_settings()
    overrides = {"system": args.system} if args.system else {}
    cfg = SystemConfig.from_yaml(Path(args.config), **overrides) if args.config else SystemConfig(**overrides)
    cfg = cfg.resolved(settings)
    dataset = Path(args.dataset)
    questions = load_dataset(dataset, include_unverified=args.include_unverified)
    if not questions:
        raise SystemExit("No questions to run (only verified questions are used; see --include-unverified)")
    questions = questions[: args.limit] if args.limit else questions

    est = None if args.mock_llm else estimate_cost(cfg.system, cfg.llm_model, settings.judge_model, len(questions))
    model_label = "mock-llm" if args.mock_llm else cfg.llm_model
    print(f"System {cfg.system} | model {model_label} | judge {'mock' if args.mock_llm else settings.judge_model} | {len(questions)} questions")
    if args.mock_llm:
        print("MOCK LLM: offline pipeline check, no API calls, results are not meaningful.")
    elif est is None:
        print("Estimated cost: unknown (model not in price table)")
    else:
        print(f"Estimated cost (pessimistic, incl. judge): ${est:.2f}")
    if not args.mock_llm and (est is None or est > COST_GUARD_USD) and not args.yes:
        raise SystemExit(f"Estimated cost exceeds ${COST_GUARD_USD:.2f} (or is unknown); re-run with --yes to proceed")

    svc = svc or build_services(settings)
    if args.mock_llm:
        from eval.mock_llm import MockProvider

        svc.provider_overrides["*"] = MockProvider()
    repo_ids = {store.repo_id_for_slug(q.repo) for q in questions}
    for q in questions:
        repo = store.get_repo(svc.conn, store.repo_id_for_slug(q.repo))
        if repo is None or repo.get("indexed_sha") != q.commit_sha:
            raise SystemExit(
                f"{q.repo} must be indexed at {q.commit_sha} first:\n"
                f"  uv run --project backend python -m app.cli index https://github.com/{q.repo} --commit {q.commit_sha}"
            )

    run_id = args.run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{cfg.system}-{Path(args.config or 'default').stem}-{dataset.stem}"
    out = raw_dir / run_id
    out.mkdir(parents=True, exist_ok=False)
    meta = {
        "run_id": run_id, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mock": bool(args.mock_llm), "config_file": args.config, "config": cfg.model_dump(),
        "model": "mock-llm" if args.mock_llm else cfg.llm_model, "judge_model": "mock" if args.mock_llm else settings.judge_model,
        "codeatlas_version": codeatlas_version(), "dataset": str(dataset.as_posix()),
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "include_unverified": bool(args.include_unverified), "question_ids": [q.id for q in questions],
        "repos": sorted(repo_ids), "commit_shas": sorted({q.commit_sha for q in questions}),
        "embedding": {"dense": settings.dense_embedding_model, "sparse": settings.sparse_embedding_model,
                      "reranker": settings.reranker_model},
        "temperature": "not supported (Anthropic Messages API rejects sampling params)"
        if cfg.llm_provider == "anthropic" else settings.llm_temperature,
        "python": platform.python_version(), "platform": platform.platform(), "estimated_cost_usd": est,
    }  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    system = get_system(cfg.system, svc)
    with (out / "answers.jsonl").open("w", encoding="utf-8") as f:
        for i, q in enumerate(questions, 1):
            t0 = time.perf_counter()
            ans = await system.answer(store.repo_id_for_slug(q.repo), q.question, cfg)  # question text only
            row = {"id": q.id, "repo": q.repo, "category": q.category, **ans.as_dict()}
            f.write(json.dumps(row, default=str) + "\n")
            f.flush()
            status = "ERROR " + str(ans.error) if ans.error else f"{ans.usage.get('tool_calls', 0)} tools"
            print(f"[{i}/{len(questions)}] {q.id} {time.perf_counter() - t0:.1f}s {status}")

    if not args.no_judge:
        from eval.judge.judge import judge_run

        await judge_run(out, mock=args.mock_llm, settings=settings)
    print(f"Saved run to {out}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eval.run")
    p.add_argument("--dataset", required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--system", default=None, choices=["naive_rag", "indexed_agent", "grep_agent"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--include-unverified", action="store_true")
    p.add_argument("--yes", action="store_true", help="confirm runs estimated above the cost guard")
    p.add_argument("--run-id", default=None)
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--mock-llm", action="store_true", help="offline pipeline check (no API calls)")
    args = p.parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
