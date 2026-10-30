"""Retrieval = vector search (dense / sparse / hybrid) + optional cross-encoder reranking."""

from __future__ import annotations

import asyncio

from app.core.index.embedder import Reranker
from app.core.index.vector_store import CODE_KINDS, DOC_KINDS, Hit, VectorStore, collection_name
from app.core.systems.config import SystemConfig


class Retriever:
    def __init__(
        self, store: VectorStore, repo_id: str, cfg: SystemConfig, reranker: Reranker | None
    ) -> None:
        self.store = store
        self.repo_id = repo_id
        self.cfg = cfg
        self.reranker = reranker if cfg.rerank else None
        self.collection = collection_name(repo_id, cfg.chunking)

    async def search(self, query: str, k: int | None = None, scope: str = "code") -> list[Hit]:
        k = k or self.cfg.top_k
        kinds = {"code": CODE_KINDS, "docs": DOC_KINDS, "all": None}[scope]
        n = max(k, self.cfg.rerank_candidates) if self.reranker else k
        hits = await self.store.search(
            self.collection, query, k=n, mode=self.cfg.search_mode, kinds=kinds
        )
        if self.reranker and hits:
            scores = await asyncio.to_thread(self.reranker.rerank, query, [h.text for h in hits])
            for h, s in zip(hits, scores, strict=True):
                h.score = s
            hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
