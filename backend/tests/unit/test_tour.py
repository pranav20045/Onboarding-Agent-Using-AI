from __future__ import annotations

import json
from pathlib import Path

from app.core.indexer import create_job, run_index_job
from app.core.llm.base import LLMError
from app.core.models import RepoRef
from app.core.tour.generator import collect_facts, get_or_build_tour
from app.db import store
from tests.fakes import ScriptedProvider, Step, make_services


async def _svc(fixture_repo: Path, tmp_path: Path, provider):
    svc = make_services(tmp_path, provider)
    ref = RepoRef("local", "shopkit")
    repo_id = store.upsert_repo(svc.conn, ref)
    await run_index_job(
        svc, repo_id, create_job(svc, repo_id), ref=ref, clone_url=str(fixture_repo)
    )
    return svc, repo_id


async def test_facts(fixture_repo: Path, tmp_path: Path) -> None:
    svc, repo_id = await _svc(fixture_repo, tmp_path, None)
    ctx = svc.repo_context(repo_id)
    facts = collect_facts(ctx, ctx.graph)
    assert facts["readme_path"] == "README.md"
    assert {"path": "shop", "python_files": 10} in facts["packages"]
    kinds = {(e["kind"], e["qualified_name"]) for e in facts["entry_points"]}
    assert ("console_script", "shop.cli.main") in kinds and (
        "route",
        "shop.api.create_order_route",
    ) in kinds
    assert any(
        m["qualified_name"] == "shop.models.Order" for m in facts["data_models"]
    )  # @dataclass
    assert facts["key_symbols"]


async def test_llm_tour_with_citation_check_and_cache(fixture_repo: Path, tmp_path: Path) -> None:
    reply = json.dumps({
        "purpose": "A tiny shop backend.",
        "steps": [
            {"title": "Order workflow", "citation": "[shop/services.py:8-27]", "explanation": "Core logic."},
            {"title": "Made up", "citation": "[shop/ghost.py:1-5]", "explanation": "Does not exist."},
        ],
    })  # fmt: skip
    provider = ScriptedProvider([Step(f"Here is the tour:\n{reply}")])
    svc, repo_id = await _svc(fixture_repo, tmp_path, provider)
    tour = await get_or_build_tour(svc, repo_id)
    assert tour["purpose"] == "A tiny shop backend."
    assert [s["citation"] for s in tour["reading_order"]] == ["[shop/services.py:8-27]"]
    assert any("ghost.py" in w for w in tour["warnings"])
    assert "untrusted data" in provider.requests[0].system
    again = await get_or_build_tour(svc, repo_id)  # served from cache: no further LLM call
    assert again == tour and len(provider.requests) == 1


class _Down:
    name, model = "anthropic", "claude-opus-5-5"

    async def chat(self, *a, **k):
        raise LLMError("Anthropic authentication failed (check ANTHROPIC_API_KEY)")


async def test_tour_without_llm_falls_back(fixture_repo: Path, tmp_path: Path) -> None:
    svc, repo_id = await _svc(fixture_repo, tmp_path, _Down())
    tour = await get_or_build_tour(svc, repo_id)
    assert tour["llm"] is None and "LLM narrative unavailable" in tour["warnings"][0]
    steps = tour["reading_order"]
    assert steps[0]["citation"].startswith("[README.md:1-")
    assert any("shop/cli.py" in s["citation"] or "shop/api.py" in s["citation"] for s in steps)
    assert all(s["explanation"] for s in steps)
