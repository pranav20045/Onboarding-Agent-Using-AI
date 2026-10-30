"""CodeAtlas evaluation harness. Run from the repository root, e.g.

    uv run --project backend python -m eval.run --system indexed_agent --dataset eval/datasets/flask.jsonl \
        --config configs/full.yaml --limit 5

The backend package (`app`) lives in backend/; it is added to sys.path here so the harness
talks to the systems only through their public interface (`System.answer`).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = str(REPO_ROOT / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
