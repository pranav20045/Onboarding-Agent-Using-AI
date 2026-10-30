"""Full vs incremental indexing must produce identical symbol tables and graphs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import networkx as nx

from app.config import Settings
from app.core.models import RepoRef
from app.core.pipeline import RepoPaths, run_structure_stages
from app.db import store
from app.db.connection import init_db


def _settings(tmp: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp / "data")  # type: ignore[call-arg]


async def _index(tmp: Path, checkout: Path, name: str, full: bool, conn=None):
    settings = _settings(tmp)
    conn = conn or init_db(tmp / f"{name}.db")
    repo_id = store.upsert_repo(conn, RepoRef("local", name))
    paths = RepoPaths(tmp / "data" / name)
    outcome, graph, stats = await run_structure_stages(
        conn, settings, repo_id, checkout, paths, full=full
    )
    return conn, repo_id, outcome, graph, stats


def _graph_fingerprint(graph) -> str:
    data = nx.node_link_data(graph.g, edges="edges")
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    data["edges"] = sorted(
        json.dumps({k: v for k, v in e.items() if k != "key"}, sort_keys=True)
        for e in data["edges"]
    )
    return json.dumps(data, sort_keys=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


async def test_incremental_equals_full(fixture_repo: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    shutil.copytree(fixture_repo, work)
    conn, repo_id, first, _, _ = await _index(tmp_path, work, "inc", full=False)
    assert first.added and not first.changed

    # Mutate the working tree: modify, add, delete and rename files.
    services = work / "shop/services.py"
    services.write_text(
        services.read_text(encoding="utf-8").replace(
            "    def checkout", "    def cancel(self, order_id: str) -> None:\n"
            "        self.orders.pop(order_id)\n\n    def checkout",
        ),
        encoding="utf-8",
    )  # fmt: skip
    (work / "shop/reports.py").write_text(
        "from shop.services import OrderService\n\n\ndef report():\n"
        "    svc = OrderService()\n    return svc.checkout('x')\n",
        encoding="utf-8",
    )
    (work / "shop/cli.py").unlink()
    (work / "shop/text_utils.py").rename(work / "shop/strings.py")
    _git(work, "add", "-A")

    _, _, inc, inc_graph, inc_stats = await _index(tmp_path, work, "inc", full=False, conn=conn)
    assert inc.changed == {"shop/services.py"}
    assert inc.added == {"shop/reports.py", "shop/strings.py"}
    assert inc.removed == {"shop/cli.py", "shop/text_utils.py"}
    assert (inc_stats.added, inc_stats.changed, inc_stats.removed) == (2, 1, 2)

    full_conn, full_id, _, full_graph, _ = await _index(tmp_path, work, "full", full=True)
    inc_syms = [row[1:] + (row[0],) for row in store.snapshot_symbols(conn, repo_id)]
    full_syms = [row[1:] + (row[0],) for row in store.snapshot_symbols(full_conn, full_id)]
    assert sorted(inc_syms) == sorted(full_syms)
    assert store.file_hashes(conn, repo_id) == store.file_hashes(full_conn, full_id)
    assert _graph_fingerprint(inc_graph) == _graph_fingerprint(full_graph)

    # The new call through a local variable resolves.
    assert inc_graph.g.has_edge(
        "sym:shop.reports.report", "sym:shop.services.OrderService.checkout"
    )
    assert not any(n.startswith("sym:shop.cli") for n in inc_graph.g)


async def test_reindex_same_tree_is_noop(fixture_repo: Path, tmp_path: Path) -> None:
    conn, repo_id, _, g1, _ = await _index(tmp_path, fixture_repo, "same", full=False)
    before = store.snapshot_symbols(conn, repo_id)
    _, _, outcome, g2, _ = await _index(tmp_path, fixture_repo, "same", full=False, conn=conn)
    assert not outcome.dirty and not outcome.removed
    assert store.snapshot_symbols(conn, repo_id) == before
    assert _graph_fingerprint(g1) == _graph_fingerprint(g2)
