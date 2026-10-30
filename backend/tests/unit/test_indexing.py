"""Vector indexing + hybrid retrieval with in-memory Qdrant and hashing embeddings."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.index.retriever import Retriever
from app.core.index.vector_store import collection_name
from app.core.indexer import create_job, get_job, run_index_job
from app.core.models import RepoRef
from app.core.systems.config import SystemConfig
from app.db import store
from tests.fakes import make_services

REF = RepoRef("local", "shopkit")


async def _index(svc, checkout: Path, full: bool = False):
    repo_id = store.upsert_repo(svc.conn, REF)
    job = create_job(svc, repo_id)
    result = await run_index_job(svc, repo_id, job, ref=REF, clone_url=str(checkout), full=full)
    return repo_id, job, result


async def test_index_job_end_to_end(fixture_repo: Path, tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    repo_id, job, result = await _index(svc, fixture_repo)
    assert get_job(svc, job)["status"] == "ready"
    repo = store.get_repo(svc.conn, repo_id)
    assert repo["status"] == "ready" and repo["indexed_sha"] == result["sha"]
    ast = result["vectors"]["ast"]
    assert ast["points"] == ast["embedded_chunks"] > 0
    assert result["vectors"]["fixed"]["points"] > 0


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
async def test_search_modes_find_the_right_code(
    fixture_repo: Path, tmp_path: Path, mode: str
) -> None:
    svc = make_services(tmp_path)
    repo_id, _, _ = await _index(svc, fixture_repo)
    cfg = SystemConfig(search_mode=mode, rerank=False)
    hits = await Retriever(svc.store, repo_id, cfg, None).search("validate order has no items", k=3)
    assert hits and hits[0].qualified_name == "shop.services.OrderService.validate_order"
    assert hits[0].citation() == "[shop/services.py:20-22]"


async def test_rerank_and_doc_scope(fixture_repo: Path, tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    repo_id, _, _ = await _index(svc, fixture_repo)
    cfg = SystemConfig(rerank=True, rerank_candidates=10)
    hits = await Retriever(svc.store, repo_id, cfg, svc.reranker).search(
        "payment gateway charge", k=2
    )
    assert hits[0].qualified_name == "shop.payments.charge"
    docs = await Retriever(svc.store, repo_id, cfg, svc.reranker).search(
        "architecture requests", scope="docs"
    )
    assert docs and all(h.kind == "doc" for h in docs)


async def test_incremental_vectors_equal_full(fixture_repo: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    shutil.copytree(fixture_repo, work)
    svc = make_services(tmp_path / "inc")
    repo_id, _, _ = await _index(svc, work)

    (work / "shop/utils.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
    (work / "shop/new_mod.py").write_text("def brand_new():\n    return 'x'\n", encoding="utf-8")
    (work / "shop/cli.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "edit"],
                   cwd=work, check=True, capture_output=True)  # fmt: skip

    _, _, inc = await _index(svc, work)
    assert inc["vectors"]["ast"]["embedded_files"] == 2  # changed + added only
    assert inc["vectors"]["ast"]["deleted_files"] == 2  # changed + removed

    full_svc = make_services(tmp_path / "full")
    full_id, _, _ = await _index(full_svc, work, full=True)
    for strategy in ("ast", "fixed"):
        a = await svc.store.all_point_ids(collection_name(repo_id, strategy))
        b = await full_svc.store.all_point_ids(collection_name(full_id, strategy))
        assert a == b, strategy
    hits = await Retriever(svc.store, repo_id, SystemConfig(rerank=False), None).search(
        "brand_new", k=1
    )
    assert hits[0].path == "shop/new_mod.py"


async def test_reindex_is_idempotent(fixture_repo: Path, tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    _, _, first = await _index(svc, fixture_repo)
    _, _, second = await _index(svc, fixture_repo)
    assert second["vectors"]["ast"]["embedded_files"] == 0
    assert second["vectors"]["ast"]["points"] == first["vectors"]["ast"]["points"]


async def test_failed_job_is_recorded(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    repo_id = store.upsert_repo(svc.conn, REF)
    job = create_job(svc, repo_id)
    with pytest.raises(Exception):  # noqa: B017
        await run_index_job(svc, repo_id, job, ref=REF, clone_url=str(tmp_path / "missing"))
    row = get_job(svc, job)
    assert row["status"] == "failed" and "Could not clone" in row["error"]
    assert store.get_repo(svc.conn, repo_id)["status"] == "failed"
