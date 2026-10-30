"""Reproduce QA-F01 / QA-F01b: concurrent first-time SQLite initialisation fails.

N processes open the same brand-new DB file at the same moment, as an API worker plus a
background index worker would on first start.

Run from backend/:   uv run python ../qa/scripts/stress_init_db.py [init_db|connect] [trials]
Exit code 1 if any process failed.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # backend/ on sys.path so `app` imports


def _worker(mode: str, path: str, barrier, queue) -> None:  # type: ignore[no-untyped-def]
    from app.db.connection import connect, init_db

    barrier.wait()
    try:
        (init_db if mode == "init_db" else connect)(path).close()
        queue.put("ok")
    except Exception as exc:  # report every failure kind
        queue.put(f"{type(exc).__name__}: {exc}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "init_db"
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    procs = 4
    failures: dict[str, int] = {}
    for _ in range(trials):
        path = str(Path(tempfile.mkdtemp(prefix="qa_race_")) / "race.db")
        barrier, queue = mp.Barrier(procs), mp.Queue()
        workers = [
            mp.Process(target=_worker, args=(mode, path, barrier, queue)) for _ in range(procs)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        for _ in range(procs):
            result = queue.get()
            if result != "ok":
                failures[result] = failures.get(result, 0) + 1
    total = sum(failures.values())
    print(f"mode={mode} trials={trials} procs/trial={procs} failed_processes={total}")
    for msg, count in sorted(failures.items(), key=lambda kv: -kv[1]):
        print(f"  {count} x {msg}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
