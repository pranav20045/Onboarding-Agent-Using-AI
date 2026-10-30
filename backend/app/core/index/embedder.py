"""Dense + sparse (BM25) embeddings and cross-encoder reranking.

Default backend is fastembed (ONNX, CPU, no API cost). Models load lazily and are cached per
process. `HashingEmbedder` / `HashingReranker` are deterministic stand-ins used by unit tests so
they never download models.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


@dataclass
class SparseVec:
    indices: list[int]
    values: list[float]


class Embedder(Protocol):
    dense_model: str
    sparse_model: str
    dim: int

    def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], list[SparseVec]]: ...

    def embed_query(self, text: str) -> tuple[list[float], SparseVec]: ...


class Reranker(Protocol):
    model: str

    def rerank(self, query: str, documents: list[str]) -> list[float]: ...


_LOCK = threading.Lock()

# Transformer attention memory grows with batch x length^2 (x heads x 4 bytes). A batch of 16
# chunks padded to the longest (~2,400 tokens) tried to allocate 4.5 GB and crashed indexing of
# fastapi. We sort by length and cap batch x length^2, so short texts still batch widely.
_ATTN_BUDGET = 400_000_000  # bytes of attention scores per batch (12 heads x 4 bytes assumed)
_CHARS_PER_TOKEN = 2.5  # conservative for code


def plan_batches(texts: list[str], max_batch: int) -> list[list[int]]:
    """Group text indices (shortest first) so each batch fits the attention-memory budget."""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    batches: list[list[int]] = []
    cur: list[int] = []
    for i in order:
        longest_tokens = max(
            len(texts[i]) / _CHARS_PER_TOKEN, 1.0
        )  # sorted ascending: i is the longest
        cost = (len(cur) + 1) * 12 * 4 * longest_tokens**2
        if cur and (len(cur) >= max_batch or cost > _ATTN_BUDGET):
            batches.append(cur)
            cur = []
        cur.append(i)
    if cur:
        batches.append(cur)
    return batches


@lru_cache(maxsize=4)
def _dense(model: str, cache_dir: str):
    from fastembed import TextEmbedding

    return TextEmbedding(model, cache_dir=cache_dir)


@lru_cache(maxsize=4)
def _sparse(model: str, cache_dir: str):
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model, cache_dir=cache_dir)


@lru_cache(maxsize=4)
def _cross(model: str, cache_dir: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model, cache_dir=cache_dir)


class FastEmbedEmbedder:
    def __init__(
        self, dense_model: str, sparse_model: str, cache_dir: Path, batch_size: int = 16
    ) -> None:
        self.dense_model = dense_model
        self.sparse_model = sparse_model
        self.cache_dir = str(cache_dir)
        self.batch_size = batch_size
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_query("probe")[0])
        return self._dim

    def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], list[SparseVec]]:
        dense_model = _dense(self.dense_model, self.cache_dir)
        dense: list[list[float] | None] = [None] * len(texts)
        with _LOCK:  # onnxruntime sessions are not shared safely across threads here
            for idx in plan_batches(texts, self.batch_size):
                for i, vec in zip(
                    idx, self._embed_resilient(dense_model, [texts[i] for i in idx]), strict=True
                ):
                    dense[i] = vec
            sparse = [
                SparseVec(s.indices.tolist(), s.values.tolist())
                for s in _sparse(self.sparse_model, self.cache_dir).embed(
                    texts, batch_size=self.batch_size
                )
            ]
        return dense, sparse  # type: ignore[return-value]

    @staticmethod
    def _embed_resilient(model, batch: list[str]) -> list[list[float]]:
        """Embed one batch; on an out-of-memory failure split it and retry (down to 1 text)."""
        try:
            return [v.tolist() for v in model.embed(batch, batch_size=len(batch))]
        except Exception as exc:
            if len(batch) == 1 or "allocate" not in str(exc).lower():
                raise
            mid = len(batch) // 2
            return FastEmbedEmbedder._embed_resilient(
                model, batch[:mid]
            ) + FastEmbedEmbedder._embed_resilient(model, batch[mid:])

    def embed_query(self, text: str) -> tuple[list[float], SparseVec]:
        with _LOCK:
            dense = next(iter(_dense(self.dense_model, self.cache_dir).query_embed(text))).tolist()
            s = next(iter(_sparse(self.sparse_model, self.cache_dir).query_embed(text)))
        return dense, SparseVec(s.indices.tolist(), s.values.tolist())


class FastEmbedReranker:
    def __init__(self, model: str, cache_dir: Path) -> None:
        self.model = model
        self.cache_dir = str(cache_dir)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        with _LOCK:
            return [float(s) for s in _cross(self.model, self.cache_dir).rerank(query, documents)]


# -- deterministic test doubles -------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        out.append(low)
        parts = [p for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", tok) if p]
        if len(parts) > 1:
            out.extend(p.lower() for p in parts)
    return out


def _bucket(token: str, n: int) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big") % n


class HashingEmbedder:
    """Bag-of-tokens hashing vectors: deterministic, fast, lexical. For tests only."""

    dense_model = "test/hashing-dense"
    sparse_model = "test/hashing-sparse"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _dense(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for t in _tokens(text):
            v[_bucket(t, self.dim)] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def _sparse(self, text: str) -> SparseVec:
        counts = Counter(_bucket(t, 2**20) for t in _tokens(text))
        idx = sorted(counts)
        return SparseVec(idx, [float(counts[i]) for i in idx])

    def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], list[SparseVec]]:
        return [self._dense(t) for t in texts], [self._sparse(t) for t in texts]

    def embed_query(self, text: str) -> tuple[list[float], SparseVec]:
        return self._dense(text), self._sparse(text)


class HashingReranker:
    model = "test/overlap-reranker"

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        q = set(_tokens(query))
        return [len(q & set(_tokens(d))) / (len(q) or 1) for d in documents]
