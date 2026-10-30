from __future__ import annotations

from pathlib import Path

from app.core.indexer import create_job, run_index_job
from app.core.models import RepoRef
from app.core.systems.config import SystemConfig
from app.core.systems.naive_rag import NaiveRAG
from app.db import store
from tests.fakes import ScriptedProvider, Step, make_services

CFG = SystemConfig(system="naive_rag", chunking="fixed", search_mode="dense", rerank=False, top_k=4)


async def _setup(fixture_repo: Path, tmp_path: Path, provider: ScriptedProvider):
    svc = make_services(tmp_path, provider)
    ref = RepoRef("local", "shopkit")
    repo_id = store.upsert_repo(svc.conn, ref)
    await run_index_job(
        svc, repo_id, create_job(svc, repo_id), ref=ref, clone_url=str(fixture_repo)
    )
    return svc, repo_id


async def test_single_call_with_retrieved_context(fixture_repo: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider(
        ["`OrderService.validate_order` rejects empty orders [shop/services.py:20-22]."]
    )
    svc, repo_id = await _setup(fixture_repo, tmp_path, provider)
    events: list[dict] = []
    ans = await NaiveRAG(svc).answer(
        repo_id, "What happens when an order has no items?", CFG, events.append
    )
    assert ans.error is None
    assert len(provider.requests) == 1  # one LLM call, no revision needed
    req = provider.requests[0]
    assert req.tools is None
    assert "# shop/services.py:" in req.messages[0].text  # retrieved excerpts with line headers
    assert "untrusted DATA" in req.system
    assert ans.verification["ok"] and ans.citations[0]["path"] == "shop/services.py"
    assert ans.usage["llm_calls"] == 1 and ans.usage["cost_usd"] > 0
    assert [e["type"] for e in events][-1] == "final"
    assert "shop/services.py" in ans.retrieved_files


async def test_revision_pass_runs_once(fixture_repo: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        Step("It calls `refund_everything` [shop/nope.py:1-5]."),
        Step("Still wrong [shop/nope.py:1-5]."),
    ])  # fmt: skip
    svc, repo_id = await _setup(fixture_repo, tmp_path, provider)
    ans = await NaiveRAG(svc).answer(repo_id, "How are refunds handled?", CFG)
    assert len(provider.requests) == 2  # answer + exactly one revision
    assert "automatic check" in provider.requests[1].messages[-1].text
    v = ans.verification
    assert v["revised"] and v["invalid_before_revision"] == 1 and v["invalid_after_revision"] == 1
    assert v["hallucinated_symbols_before"] == 1
    assert ans.usage["llm_calls"] == 2


async def test_revision_fixes_answer(fixture_repo: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        Step("See [shop/services.py:20-99]."),
        Step("See [shop/services.py:20-22]."),
    ])  # fmt: skip
    svc, repo_id = await _setup(fixture_repo, tmp_path, provider)
    ans = await NaiveRAG(svc).answer(repo_id, "Where is validation?", CFG)
    assert ans.answer == "See [shop/services.py:20-22]."
    assert ans.verification["ok"] and ans.verification["invalid_after_revision"] == 0
