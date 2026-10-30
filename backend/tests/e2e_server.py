"""Backend for the Playwright smoke tests: the real FastAPI app with offline stand-ins.

In-memory Qdrant, hashing embeddings, the deterministic mock LLM, and
https://github.com/acme/shopkit mapped to the locally built fixture repo. No network, no keys.

    uv run python -m tests.e2e_server --port 8001
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for `eval.mock_llm`

from eval.mock_llm import MockProvider

from app.main import create_app
from tests.fakes import make_settings, services_for
from tests.fixtures.build_fixture_repo import build_fixture_repo

# A real 1x1 PNG, so the browser's <img> decodes it (naturalWidth > 0).
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def seed_results(results: Path) -> None:
    """Fixture results so the Results page can be tested without running an evaluation."""
    results.mkdir(parents=True, exist_ok=True)
    agg = {"n": 3, "mean_score": 1.0, "pct_correct": 0.5, "refusal_rate_unanswerable": None,
           "recall@5": 0.8, "gold_citation_hit_rate": 0.7, "mean_cost_usd": 0.0123,
           "latency_p50_s": 4.2, "latency_p95_s": 9.9, "mean_tool_calls": 3.0}  # fmt: skip
    summary = {
        "runs": [{"run_id": "e2e-fixture", "label": "full", "model": "fixture", "judge_model": "fixture",
                  "n": 3, "mock": True}],
        "aggregate": {"full": agg},
        "by_category": {"full": {"locate": {"n": 3, "mean_score": 1.0}}},
        "by_repo": {}, "fairness_warnings": [], "charts": ["correctness_vs_cost.png"],
    }  # fmt: skip
    (results / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (results / "correctness_vs_cost.png").write_bytes(PNG_1X1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="codeatlas-e2e-"))
    fixture = build_fixture_repo(tmp / "shopkit")
    seed_results(tmp / "results")
    settings = make_settings(tmp, history_prelink_commits=0, cors_origins=["http://localhost:3100"],
                             rate_limit_ask_per_minute=1000, rate_limit_repos_per_minute=1000)  # fmt: skip
    app = create_app(settings, lambda s: services_for(s, MockProvider(stream_delay=0.15)), results_dir=tmp / "results",
                     clone_overrides={"acme__shopkit": str(fixture)})  # fmt: skip
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
