"""LLM-as-judge with a written rubric. The judge model is configured separately
(JUDGE_PROVIDER / JUDGE_MODEL) from the system model and is fixed for a whole run.

The judge sees only: the question, the reference answer, the category and the system answer.

    uv run --project backend python -m eval.judge.judge eval/results/raw/<run_id>
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import eval  # noqa: F401  (sets up the backend import path)
from app.config import get_settings
from app.core.llm.base import LLMError, Message, get_provider
from eval import REPO_ROOT
from eval.datasets.schema import load_dataset


def _dataset_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path

RUBRIC = """You grade answers to questions about a code repository against a reference answer.

Scores:
2 = correct and complete: the main points of the reference answer are present and nothing \
important is wrong. Extra correct detail is fine. Wording does not matter.
1 = partially correct: some key points are right, but something important is missing or \
there is a minor factual error.
0 = wrong: the main claim is incorrect, the answer is about something else, it contradicts \
the reference, or it invents code/behaviour/history that does not exist.

Special cases:
- Category "unanswerable" (the thing asked about does not exist or cannot be determined): \
score 2 if the answer clearly says it does not exist / cannot be determined; 0 if it \
describes the thing as if it existed or invents a rationale; 1 if it hedges but still \
speculates substantially.
- Category "why": the reason must match the recorded rationale in the reference (commit/PR). \
A plausible but invented rationale scores 0.
- An answer that only says it could not find the answer, for an answerable question, scores 0.
- Ignore citation formatting; judge the substance.

Also report "refused": true if the answer's overall conclusion is that the requested \
thing/answer could not be found or does not exist, else false.

Return ONLY a JSON object: {"score": 0|1|2, "refused": true|false, "justification": "<one line>"}"""


def judge_prompt(question: str, category: str, reference: str, answer: str) -> str:
    return (f"Category: {category}\n\nQuestion:\n{question}\n\nReference answer:\n{reference}\n\n"
            f"System answer to grade:\n{answer or '(empty answer)'}")  # fmt: skip


def parse_judgment(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if d.get("score") not in (0, 1, 2) or not isinstance(d.get("refused"), bool):
        return None
    return {"score": d["score"], "refused": d["refused"], "justification": str(d.get("justification", ""))[:400]}


async def judge_one(provider, q, answer: str) -> dict:
    msg = [Message(role="user", text=judge_prompt(q.question, q.category, q.reference_answer, answer))]
    for _attempt in range(2):
        resp = await provider.chat(RUBRIC, msg, max_tokens=600)
        parsed = parse_judgment(resp.text)
        if parsed:
            return {**parsed, "judge_usage": {"input_tokens": resp.usage.input_tokens,
                                              "output_tokens": resp.usage.output_tokens,
                                              "cost_usd": resp.usage.cost_usd}}  # fmt: skip
    return {"score": None, "refused": None, "justification": "judge output unparseable", "judge_usage": {}}


async def judge_run(run_dir: Path, mock: bool = False, settings=None) -> Path:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    questions = {q.id: q for q in load_dataset(_dataset_path(meta["dataset"]), include_unverified=True)}
    answers = [json.loads(line) for line in (run_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines() if line]
    out = run_dir / "judgments.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text(encoding="utf-8").splitlines() if line}
    settings = settings or get_settings()
    provider = None if mock else get_provider(settings, model=settings.judge_model, provider=settings.judge_provider)
    with out.open("a", encoding="utf-8") as f:
        for a in answers:
            if a["id"] in done:
                continue
            q = questions[a["id"]]
            if mock:
                from eval.mock_llm import mock_judgment

                j = mock_judgment(a.get("answer", ""), q.gold_files, q.category)
            elif a.get("error"):
                j = {"score": 0, "refused": False, "justification": f"system error: {a['error']}"}
            else:
                try:
                    j = await judge_one(provider, q, a.get("answer", ""))
                except LLMError as exc:
                    j = {"score": None, "refused": None, "justification": f"judge error: {exc}"}
            f.write(json.dumps({"id": a["id"], "judge_model": "mock" if mock else settings.judge_model, **j}) + "\n")
            f.flush()
    return out


if __name__ == "__main__":
    asyncio.run(judge_run(Path(sys.argv[1]), mock="--mock" in sys.argv))
