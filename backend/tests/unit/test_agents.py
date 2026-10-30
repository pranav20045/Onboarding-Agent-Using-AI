"""Agent loop + indexed_agent / grep_agent with a scripted LLM (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.indexer import create_job, run_index_job
from app.core.models import RepoRef
from app.core.systems.config import SystemConfig
from app.core.systems.grep_agent import GrepAgent
from app.core.systems.indexed_agent import IndexedAgent
from app.db import store
from tests.fakes import ScriptedProvider, Step, make_services

FINAL = (
    "`OrderService.validate_order` raises `ValueError` for empty orders [shop/services.py:20-22]."
)


async def _svc(fixture_repo: Path, tmp_path: Path, steps: list, **settings_kw):
    provider = ScriptedProvider(steps)
    svc = make_services(tmp_path, provider, **settings_kw)
    ref = RepoRef("local", "shopkit")
    repo_id = store.upsert_repo(svc.conn, ref)
    from app.core.history.stage import history_stage

    await run_index_job(svc, repo_id, create_job(svc, repo_id), ref=ref, clone_url=str(fixture_repo),
                        history_stage=history_stage)  # fmt: skip
    return svc, repo_id, provider


def _tool_results(provider: ScriptedProvider, request_index: int):
    return provider.requests[request_index].messages[-1].tool_results


async def test_indexed_agent_happy_path(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [
        Step(calls=[{"name": "search_code", "args": {"query": "validate order empty"}}]),
        Step(
            calls=[
                {"name": "read_file", "args": {"path": "shop/services.py", "start": 20, "end": 22}}
            ]
        ),
        Step(FINAL),
    ]
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    events: list[dict] = []
    ans = await IndexedAgent(svc).answer(
        repo_id, "What if an order is empty?", SystemConfig(), events.append
    )
    assert ans.error is None and ans.answer == FINAL and not ans.budget_exhausted
    assert ans.verification["ok"] and ans.usage["tool_calls"] == 2 and ans.usage["llm_calls"] == 3
    assert "[shop/services.py:20-22] method shop.services.OrderService.validate_order" in \
        _tool_results(provider, 1)[0].content  # fmt: skip
    assert "20| " in _tool_results(provider, 2)[0].content  # numbered lines
    assert ans.retrieved_files[0] == "shop/services.py"
    types = [e["type"] for e in events]
    assert types.count("tool_call") == 2 and types[-1] == "final"
    labels = [e["label"] for e in events if e["type"] == "tool_call"]
    assert labels == ["Searching code: validate order empty", "Reading shop/services.py:20-22"]
    tools = {t.name for t in provider.requests[0].tools}
    assert {"search_code", "search_docs", "read_file", "list_directory", "find_definition", "find_callers",
            "find_callees", "repo_overview", "git_blame", "get_commit"} == tools  # fmt: skip


async def test_step_budget_forces_answer(fixture_repo: Path, tmp_path: Path) -> None:
    loop_call = Step(calls=[{"name": "list_directory", "args": {"path": "shop"}}])
    steps = [loop_call, loop_call, Step(FINAL)]
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig(max_tool_calls=2))
    assert ans.budget_exhausted and ans.usage["tool_calls"] == 2
    assert provider.requests[-1].tool_choice == "none"
    assert "used your tool budget" in provider.requests[-1].messages[-1].text
    assert any(e["type"] == "budget_exhausted" for e in ans.trace)


async def test_token_budget_forces_answer(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [Step(calls=[{"name": "repo_overview"}], input_tokens=500), Step(FINAL)]
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig(max_tokens=300))
    assert ans.budget_exhausted and provider.requests[1].tool_choice == "none"
    reason = next(e for e in ans.trace if e["type"] == "budget_exhausted")["reason"]
    assert reason == "token budget"


async def test_parallel_calls_beyond_budget_not_executed(
    fixture_repo: Path, tmp_path: Path
) -> None:
    three = Step(calls=[{"name": "list_directory", "args": {}}] * 3)
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, [three, Step(FINAL)])
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig(max_tool_calls=2))
    results = _tool_results(provider, 1)
    assert [r.is_error for r in results] == [False, False, True]
    assert ans.usage["tool_calls"] == 2


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ({"name": "read_file", "args": {"path": "../../etc/passwd"}}, "'..' is not allowed"),
        ({"name": "read_file", "args": {"path": "/etc/passwd"}}, "absolute paths"),
        ({"name": "read_file", "args": {"path": ".git/config"}}, ".git is not allowed"),
        ({"name": "read_file", "args": {"path": "shop/nope.py"}}, "not an indexed file"),
        ({"name": "read_file", "args": {}}, "missing required argument"),
        (
            {"name": "read_file", "args": {"path": "shop/api.py", "start": "x"}},
            "must be of type integer",
        ),
        ({"name": "read_file", "args": {"path": "shop/api.py", "bogus": 1}}, "unknown argument"),
        ({"name": "delete_everything", "args": {}}, "Unknown tool"),
        ({"name": "get_commit", "args": {"sha": "--all"}}, "4-40 hex"),
    ],
)
async def test_tool_errors_return_to_model(
    fixture_repo: Path, tmp_path: Path, call: dict, expected: str
) -> None:
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, [Step(calls=[call]), Step(FINAL)])
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig())
    (result,) = _tool_results(provider, 1)
    assert result.is_error and expected in result.content
    assert ans.error is None and ans.answer == FINAL


async def test_tool_output_truncated(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [Step(calls=[{"name": "read_file", "args": {"path": "shop/api.py"}}]), Step(FINAL)]
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig(tool_output_max_chars=200))
    content = _tool_results(provider, 1)[0].content
    assert "output truncated" in content and len(content) < 400
    assert next(e for e in ans.trace if e["type"] == "tool_result_summary")["truncated"]


async def test_ablation_flags_remove_tools(fixture_repo: Path, tmp_path: Path) -> None:
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, [Step(FINAL)])
    await IndexedAgent(svc).answer(
        repo_id, "q", SystemConfig(graph_tools=False, history_tools=False)
    )
    names = {t.name for t in provider.requests[0].tools}
    assert not names & {
        "find_definition",
        "find_callers",
        "find_callees",
        "git_blame",
        "get_commit",
    }
    assert {"search_code", "read_file", "repo_overview"} <= names


async def test_graph_and_history_tools(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [
        Step(calls=[{"name": "find_callers", "args": {"symbol": "validate_order"}},
                    {"name": "find_definition", "args": {"symbol": "charge"}},
                    {"name": "git_blame", "args": {"path": "shop/payments.py", "start": 6, "end": 6}}]),
        Step(calls=[{"name": "get_commit", "args": {"sha": "HEAD"}}]),
        Step(FINAL),
    ]  # fmt: skip
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    await IndexedAgent(svc).answer(repo_id, "Why 3 retries?", SystemConfig())
    callers, definition, blame = (r.content for r in _tool_results(provider, 1))
    assert "shop.services.OrderService.create_order [shop/services.py:14-18]" in callers
    assert "[shop/payments.py:6-11] function shop.payments.charge" in definition
    assert "Retry payment gateway calls 3 times" in blame
    get_commit = _tool_results(provider, 2)[0]
    assert get_commit.is_error  # "HEAD" is not a hex sha: rejected before reaching git


async def test_grep_agent_same_budgets_and_tools(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [
        Step(
            calls=[
                {"name": "grep", "args": {"pattern": r"def validate_order", "path_glob": "**/*.py"}}
            ]
        ),
        Step(calls=[{"name": "git_log", "args": {"path": "shop/payments.py", "max_count": 5}}]),
        Step(FINAL),
    ]
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, steps)
    ans = await GrepAgent(svc).answer(repo_id, "q", SystemConfig(system="grep_agent"))
    names = {t.name for t in provider.requests[0].tools}
    assert names == {"grep", "read_file", "list_directory", "git_log", "git_blame", "git_show"}
    grep_out = _tool_results(provider, 1)[0].content
    assert "shop/services.py:20:    def validate_order" in grep_out
    assert "Retry payment gateway calls 3 times" in _tool_results(provider, 2)[0].content
    assert ans.verification["ok"]
    cfg = ans.config
    idx_cfg = SystemConfig().resolved(svc.settings)
    for k in ("max_tool_calls", "max_tokens", "tool_output_max_chars", "llm_model", "llm_provider",
              "answer_max_tokens"):  # fmt: skip
        assert cfg[k] == getattr(idx_cfg, k), k


async def test_grep_agent_github_flag_adds_tool(fixture_repo: Path, tmp_path: Path) -> None:
    svc, repo_id, provider = await _svc(fixture_repo, tmp_path, [Step(FINAL)])
    await GrepAgent(svc).answer(
        repo_id, "q", SystemConfig(system="grep_agent", grep_agent_github=True)
    )
    assert "github_pr_for_commit" in {t.name for t in provider.requests[0].tools}


async def test_answer_is_streamed_token_by_token(fixture_repo: Path, tmp_path: Path) -> None:
    steps = [
        Step("Let me search first.", calls=[{"name": "list_directory", "args": {}}]),
        Step(FINAL),
    ]
    svc, repo_id, _provider = await _svc(fixture_repo, tmp_path, steps)
    live: list[dict] = []
    ans = await IndexedAgent(svc).answer(repo_id, "q", SystemConfig(), live.append)
    types = [e["type"] for e in live]
    deltas = [e for e in live if e["type"] == "answer_delta"]
    assert len(deltas) > 3 and all(len(d["text"]) <= 7 for d in deltas)  # many small chunks
    # Turn 1 narration streams, then ends with tool calls (final=False); turn 2 is the answer.
    ends = [e["final"] for e in live if e["type"] == "turn_end"]
    assert ends == [False, True]
    first_call = types.index("tool_call")
    assert "".join(d["text"] for d in deltas[:first_call]).startswith("Let me search")
    answer_text = "".join(d["text"] for d in deltas if live.index(d) > first_call)
    assert answer_text == FINAL
    # Deltas go to the live sink only: the stored trace stays small.
    assert not any(e["type"] == "answer_delta" for e in ans.trace)
    assert [e["final"] for e in ans.trace if e["type"] == "turn_end"] == [False, True]


async def test_naive_rag_streams(fixture_repo: Path, tmp_path: Path) -> None:
    from app.core.systems.naive_rag import NaiveRAG

    svc, repo_id, _ = await _svc(fixture_repo, tmp_path, [Step(FINAL)])
    live: list[dict] = []
    cfg = SystemConfig(system="naive_rag", chunking="fixed", search_mode="dense", rerank=False)
    ans = await NaiveRAG(svc).answer(repo_id, "q", cfg, live.append)
    assert "".join(e["text"] for e in live if e["type"] == "answer_delta") == FINAL == ans.answer
