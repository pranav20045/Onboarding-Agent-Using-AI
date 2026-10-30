"""Length-aware embedding batches (regression: a 16-chunk batch asked ONNX for 4.5 GB)."""

from __future__ import annotations

import numpy as np
import pytest

from app.core.index.embedder import _ATTN_BUDGET, _CHARS_PER_TOKEN, FastEmbedEmbedder, plan_batches


def test_plan_covers_every_text_once_and_respects_limits() -> None:
    texts = ["x" * n for n in (100, 200, 6000, 6000, 6000, 300, 50, 4000, 5999)]
    batches = plan_batches(texts, max_batch=16)
    assert sorted(i for b in batches for i in b) == list(range(len(texts)))
    for b in batches:
        longest = max(len(texts[i]) for i in b) / _CHARS_PER_TOKEN
        assert len(b) == 1 or len(b) * 12 * 4 * longest**2 <= _ATTN_BUDGET
    assert max(len(b) for b in plan_batches(["x" * 400] * 100, max_batch=16)) == 16
    assert plan_batches([], 16) == []


def test_long_chunks_never_batch_together() -> None:
    batches = plan_batches(["x" * 6000] * 5, max_batch=16)
    assert all(len(b) == 1 for b in batches)  # two 6000-char chunks would exceed the budget


class _OomOnBig:
    """Fake ONNX model: fails to allocate for batches over 2 texts."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts, batch_size):
        self.batch_sizes.append(len(texts))
        if len(texts) > 2:
            raise RuntimeError("BFCArena::AllocateRawInternal Failed to allocate memory")
        return [np.array([float(len(t))]) for t in texts]


def test_oom_retries_with_smaller_batches_and_keeps_order() -> None:
    model = _OomOnBig()
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    out = FastEmbedEmbedder._embed_resilient(model, texts)
    assert out == [[1.0], [2.0], [3.0], [4.0], [5.0]]
    assert model.batch_sizes[0] == 5 and max(model.batch_sizes[1:]) <= 3


def test_non_oom_errors_are_not_swallowed() -> None:
    class Broken:
        def embed(self, texts, batch_size):
            raise ValueError("bad input")

    with pytest.raises(ValueError):
        FastEmbedEmbedder._embed_resilient(Broken(), ["a", "b"])
