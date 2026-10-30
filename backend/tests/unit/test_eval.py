"""Eval harness: hand-computed metrics, leakage, mock end-to-end run, cost guard, summary."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from eval.datasets.schema import Question, save_dataset, validate
from eval.judge.agreement import cohen_kappa
from eval.judge.judge import parse_judgment
from eval.metrics.compute import aggregate, per_question, percentile
from eval.run import run
from eval.summarize import build

from app.core.indexer import create_job, run_index_job
from app.core.models import RepoRef
from app.db import store
from tests.fakes import ScriptedProvider, Step, make_services

Q = {"gold_files": ["a.py", "b.py"], "gold_symbols": ["Foo.bar"]}


def _ans(system: str, retrieved: list[str], cited: list[str], cost: float, latency: float, cat: str = "locate",
         symbols: list[str] | None = None) -> dict:  # fmt: skip
    return {"id": "x-001", "repo": "o/r", "category": cat, "system": system, "retrieved_files": retrieved,
            "retrieved_symbols": symbols or [], "citations": [{"path": p} for p in cited],
            "verification": {"citations_total": len(cited), "invalid_after_revision": 0},
            "usage": {"cost_usd": cost, "total_tokens": 100, "latency_s": latency, "tool_calls": 2}}  # fmt: skip


def test_metrics_hand_computed() -> None:
    r1 = per_question(_ans("indexed_agent", ["a.py", "c.py", "d.py", "e.py", "f.py", "b.py"], ["a.py"], 0.10, 1.0,
                           symbols=["pkg.Foo.bar"]), Q, {"score": 2, "refused": False})  # fmt: skip
    assert r1["recall@5"] == 0.5 and r1["recall@10"] == 1.0  # b.py is 6th
    assert (
        r1["symbol_recall@5"] == 1.0 and r1["gold_citation_hit"] is True and r1["correct"] is True
    )
    r2 = per_question(
        _ans("grep_agent", ["a.py"], ["z.py"], 0.30, 3.0), Q, {"score": 1, "refused": False}
    )
    assert r2["recall@5"] is None and r2["gold_citation_hit"] is False  # no retrieval step
    r3 = per_question(_ans("naive_rag", [], [], 0.02, 2.0, cat="unanswerable"), {"gold_files": []},
                      {"score": 2, "refused": True})  # fmt: skip
    r4 = per_question(_ans("naive_rag", [], [], 0.02, 10.0, cat="unanswerable"), {"gold_files": []},
                      {"score": 0, "refused": False})  # fmt: skip
    agg = aggregate([r1, r2, r3, r4])
    assert agg["mean_score"] == pytest.approx(5 / 4)
    assert agg["pct_correct"] == pytest.approx(0.5)
    assert agg["refusal_rate_unanswerable"] == pytest.approx(0.5)
    assert agg["false_refusal_rate"] == 0.0
    assert agg["recall@5"] == 0.5  # only r1 has a retrieval recall
    assert agg["gold_citation_hit_rate"] == 0.5  # r1 yes, r2 no; r3/r4 have no gold files
    assert agg["mean_cost_usd"] == pytest.approx(0.11) and agg["total_cost_usd"] == pytest.approx(
        0.44
    )
    assert agg["latency_p50_s"] == pytest.approx(2.5) and agg["latency_p95_s"] == pytest.approx(
        8.95
    )  # [1,2,3,10]: 3 + 0.85*7
    assert percentile([], 50) is None


def test_kappa_and_judge_parsing() -> None:
    assert cohen_kappa([0, 1, 2], [0, 1, 2]) == 1.0
    assert cohen_kappa([0, 1, 2, 2, 1, 0], [0, 2, 2, 1, 1, 0]) == pytest.approx(0.5)
    assert (
        parse_judgment('ok {"score": 2, "refused": false, "justification": "fine"}')["score"] == 2
    )
    assert parse_judgment('{"score": 3, "refused": false}') is None
    assert parse_judgment("no json") is None


@pytest.fixture
async def indexed(fixture_repo: Path, tmp_path: Path):
    provider = ScriptedProvider(
        [Step("Validation is in `OrderService.validate_order` [shop/services.py:20-22].")] * 4
    )
    svc = make_services(tmp_path, provider)
    ref = RepoRef("local", "shopkit")
    repo_id = store.upsert_repo(svc.conn, ref)
    await run_index_job(
        svc, repo_id, create_job(svc, repo_id), ref=ref, clone_url=str(fixture_repo)
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fixture_repo, capture_output=True, text=True
    ).stdout.strip()
    secret = "REFERENCE-ANSWER-SECRET-TOKEN"
    qs = [
        Question(id="shop-001", repo="local/shopkit", commit_sha=sha, category="locate",
                 question="Where are orders validated?", reference_answer=f"In validate_order. {secret}",
                 gold_files=["shop/services.py"], gold_symbols=["OrderService.validate_order"],
                 source="manual", verified=True),
        Question(id="shop-002", repo="local/shopkit", commit_sha=sha, category="unanswerable",
                 question="Where is the refund workflow?", reference_answer=f"There is none. {secret}",
                 source="manual", verified=True),
        Question(id="shop-003", repo="local/shopkit", commit_sha=sha, category="locate",
                 question="Unverified draft?", reference_answer="x" * 20, gold_files=["shop/api.py"],
                 source="llm_draft", verified=False),
    ]  # fmt: skip
    ds = tmp_path / "shop.jsonl"
    save_dataset(ds, qs)
    return svc, provider, ds, secret, fixture_repo


def _args(ds: Path, **kw) -> argparse.Namespace:
    base = {"dataset": str(ds), "config": None, "system": "naive_rag", "limit": None, "include_unverified": False,
            "yes": False, "run_id": None, "no_judge": True, "mock_llm": False}  # fmt: skip
    return argparse.Namespace(**{**base, **kw})


async def test_runner_no_leakage_and_verified_only(indexed, tmp_path: Path) -> None:
    svc, provider, ds, secret, _ = indexed
    out = await run(_args(ds, yes=True), svc=svc, raw_dir=tmp_path / "raw")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["question_ids"] == ["shop-001", "shop-002"]  # unverified excluded
    assert meta["config"]["max_tool_calls"] and meta["model"] and meta["codeatlas_version"]
    assert meta["temperature"].startswith("not supported")
    sent = json.dumps([[m.text for m in r.messages] + [r.system] for r in provider.requests])
    assert (
        secret not in sent and "gold_files" not in sent
    )  # nothing from the dataset but the question
    rows = [json.loads(x) for x in (out / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["shop-001", "shop-002"] and rows[0]["answer"]


async def test_cost_guard(indexed, tmp_path: Path) -> None:
    svc, _, ds, _, _ = indexed
    svc.settings.llm_model = "unknown-model"  # unknown price -> guard requires --yes
    with pytest.raises(SystemExit, match="--yes"):
        await run(_args(ds), svc=svc, raw_dir=tmp_path / "raw")


async def test_mock_pipeline_and_summary(indexed, tmp_path: Path) -> None:
    svc, _, ds, _, _ = indexed
    raw = tmp_path / "raw"
    for system in ("naive_rag", "indexed_agent", "grep_agent"):
        await run(_args(ds, system=system, mock_llm=True, no_judge=False, include_unverified=True,
                        run_id=f"mock-{system}"), svc=svc, raw_dir=raw)  # fmt: skip
    for system in ("naive_rag", "indexed_agent", "grep_agent"):
        judged = (
            (raw / f"mock-{system}" / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
        )
        assert len(judged) == 3
    assert build(include_mock=False, out=tmp_path / "res", raw=raw)["runs"] == []
    s = build(include_mock=True, out=tmp_path / "res", raw=raw)
    assert {r["label"] for r in s["runs"]} == {"naive_rag", "indexed_agent", "grep_agent"}
    md = (tmp_path / "res" / "summary.md").read_text(encoding="utf-8")
    assert "MOCK runs" in md and "## Main comparison" in md
    assert s["aggregate"]["indexed_agent"]["n"] == 3


def test_dataset_validator_catches_problems(indexed, tmp_path: Path) -> None:
    *_, fixture_repo = indexed
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fixture_repo, capture_output=True, text=True
    ).stdout.strip()
    bad = [
        Question(id="bad-001", repo="local/shopkit", commit_sha=sha, category="locate", question="Where is X defined?",
                 reference_answer="In a file that does not exist.", gold_files=["shop/nope.py"],
                 gold_symbols=["Nope"], source="llm_draft"),
        Question(id="bad-002", repo="local/shopkit", commit_sha=sha, category="why", question="Why 3 retries here?",
                 reference_answer="Because reasons, no commit cited.", gold_files=["shop/payments.py"],
                 source="llm_draft"),
    ]  # fmt: skip
    path = tmp_path / "bad.jsonl"
    save_dataset(path, bad)
    problems = "\n".join(validate(path, fixture_repo))
    assert "gold file shop/nope.py does not exist" in problems
    assert "must cite a real commit sha" in problems
