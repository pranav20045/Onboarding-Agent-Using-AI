"""Developer CLI.

uv run python -m app.cli index https://github.com/psf/requests [--commit SHA] [--full]
uv run python -m app.cli index-local path/to/repo --name shopkit
uv run python -m app.cli ask psf__requests "How are retries configured?" [--system grep_agent]
uv run python -m app.cli stats psf__requests
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.core.ingest.url import parse_github_url
from app.core.models import RepoRef
from app.core.services import build_services
from app.core.systems.config import SystemConfig
from app.db import store


def _print_event(event: dict) -> None:
    t = event["type"]
    if t == "tool_call":
        print(f"  -> {event['tool']}({json.dumps(event.get('args', {}))[:150]})", file=sys.stderr)
    elif t == "tool_result_summary":
        print(f"  <- {event.get('summary', '')[:150]}", file=sys.stderr)
    elif t in ("verification", "revision", "budget_exhausted", "error"):
        print(
            f"  [{t}] {json.dumps({k: v for k, v in event.items() if k not in ('type', 't')})}",
            file=sys.stderr,
        )


async def cmd_index(args: argparse.Namespace) -> int:
    from app.core.history.stage import history_stage
    from app.core.indexer import create_job, run_index_job

    svc = build_services()
    if args.command == "index":
        ref, clone_url = parse_github_url(args.url), None
    else:
        local = Path(args.path).resolve()
        ref, clone_url = RepoRef(owner="local", name=args.name or local.name), str(local)
    repo_id = store.upsert_repo(svc.conn, ref, pinned_sha=args.commit)
    job_id = create_job(svc, repo_id, "full" if args.full else "incremental", args.commit)

    last = [""]

    def watch() -> None:
        row = svc.conn.execute("SELECT status, progress_done, progress_total FROM index_jobs WHERE id = ?",
                               (job_id,)).fetchone()  # fmt: skip
        line = f"[{row[0]}] {row[1]}/{row[2]}" if row[2] else f"[{row[0]}]"
        if line != last[0]:
            print("  " + line, file=sys.stderr)
            last[0] = line

    async def poll() -> None:
        while True:
            watch()
            await asyncio.sleep(1)

    poller = asyncio.create_task(poll())
    try:
        result = await run_index_job(
            svc, repo_id, job_id, ref=ref, commit_sha=args.commit, clone_url=clone_url, full=args.full,
            history_stage=None if args.no_history else history_stage,
        )  # fmt: skip
    finally:
        poller.cancel()
    print(json.dumps(result, indent=2, default=str))
    return 0


async def cmd_ask(args: argparse.Namespace) -> int:
    from app.core.systems.registry import get_system

    svc = build_services()
    if store.get_repo(svc.conn, args.repo_id) is None:
        print(f"Unknown repo {args.repo_id}", file=sys.stderr)
        return 1
    cfg = SystemConfig.from_yaml(Path(args.config), system=args.system) if args.config else SystemConfig(
        system=args.system or "indexed_agent"
    )  # fmt: skip
    system = get_system(cfg.system, svc)
    ans = await system.answer(args.repo_id, args.question, cfg, on_event=_print_event)
    if args.json:
        print(json.dumps(ans.as_dict(), indent=2, default=str))
    else:
        print(ans.answer or f"ERROR: {ans.error}")
        print("\n--- verification:", json.dumps({k: v for k, v in ans.verification.items()
                                                 if k not in ("hallucinated_symbols", "invalid_citations")}))  # fmt: skip
        print("--- usage:", json.dumps(ans.usage))
    return 0 if ans.error is None else 2


def cmd_stats(args: argparse.Namespace) -> int:
    from app.db.connection import init_db

    conn = init_db(get_settings().db_path)
    repo = store.get_repo(conn, args.repo_id)
    if repo is None:
        print(f"Unknown repo {args.repo_id}", file=sys.stderr)
        return 1
    print(json.dumps(repo, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="codeatlas")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_ in (("index", "Clone and index a public GitHub repo"),
                        ("index-local", "Index a local git repo (development only)")):  # fmt: skip
        p = sub.add_parser(name, help=help_)
        if name == "index":
            p.add_argument("url")
        else:
            p.add_argument("path")
            p.add_argument("--name", default=None)
        p.add_argument("--commit", default=None)
        p.add_argument("--full", action="store_true", help="ignore previous index state")
        p.add_argument("--no-history", action="store_true", help="skip commit/PR history mining")
        p.set_defaults(func=cmd_index, is_async=True)

    p = sub.add_parser("ask", help="Ask a question about an indexed repo")
    p.add_argument("repo_id")
    p.add_argument("question")
    p.add_argument("--system", default=None, choices=["naive_rag", "indexed_agent", "grep_agent"])
    p.add_argument("--config", default=None, help="YAML SystemConfig (e.g. configs/full.yaml)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask, is_async=True)

    p = sub.add_parser("stats", help="Print stored stats for a repo")
    p.add_argument("repo_id")
    p.set_defaults(func=cmd_stats, is_async=False)

    args = parser.parse_args(argv)
    if args.is_async:
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
