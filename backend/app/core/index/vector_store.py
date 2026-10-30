"""Qdrant storage and hybrid search.

One collection per (repo, chunking strategy), e.g. `codeatlas__psf__requests__ast`. Each point
has a named dense vector and a named sparse vector (BM25, IDF modifier applied by Qdrant).
Hybrid search runs both as prefetches and fuses them with reciprocal rank fusion server-side.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from qdrant_client import AsyncQdrantClient, models

from app.core.index.embedder import Embedder, SparseVec
from app.core.models import Chunk, stable_id

SearchMode = Literal["dense", "sparse", "hybrid"]
DENSE = "dense"
SPARSE = "sparse"
CODE_KINDS = ["code", "text_fallback", "fixed"]
DOC_KINDS = ["doc"]


@dataclass
class Hit:
    chunk_id: str
    path: str
    start_line: int
    end_line: int
    kind: str
    qualified_name: str | None
    unit_kind: str | None
    text: str
    score: float

    def citation(self) -> str:
        return f"[{self.path}:{self.start_line}-{self.end_line}]"


def collection_name(repo_id: str, strategy: str) -> str:
    return f"codeatlas__{repo_id}__{strategy}"


def point_id(repo_id: str, strategy: str, chunk_id: str) -> str:
    return stable_id(repo_id, strategy, chunk_id)


class VectorStore:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder) -> None:
        self.client = client
        self.embedder = embedder

    async def ensure_collection(self, name: str) -> None:
        if await self.client.collection_exists(name):
            return
        await self.client.create_collection(
            name,
            vectors_config={
                DENSE: models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        for field, schema in (
            ("path", models.PayloadSchemaType.KEYWORD),
            ("kind", models.PayloadSchemaType.KEYWORD),
        ):
            await self.client.create_payload_index(name, field_name=field, field_schema=schema)

    async def delete_collection(self, name: str) -> None:
        if await self.client.collection_exists(name):
            await self.client.delete_collection(name)

    async def delete_paths(self, name: str, paths: set[str]) -> None:
        if not paths or not await self.client.collection_exists(name):
            return
        sel = models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="path", match=models.MatchAny(any=sorted(paths)))]
            )
        )
        await self.client.delete(name, points_selector=sel, wait=True)

    async def upsert_chunks(self, name: str, repo_id: str, strategy: str, chunks: list[Chunk], batch: int = 64,
                            progress=None) -> int:  # fmt: skip
        done = 0
        for i in range(0, len(chunks), batch):
            part = chunks[i : i + batch]
            # CPU-bound ONNX inference: keep it off the event loop
            dense, sparse = await asyncio.to_thread(
                self.embedder.embed_documents, [c.text for c in part]
            )
            points = [
                models.PointStruct(
                    id=point_id(repo_id, strategy, c.id),
                    vector={
                        DENSE: d,
                        SPARSE: models.SparseVector(indices=s.indices, values=s.values),
                    },
                    payload={
                        "chunk_id": c.id,
                        "path": c.path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "kind": c.kind,
                        "qualified_name": c.qualified_name,
                        "unit_kind": c.unit_kind,
                        "text": c.text,
                        "content_hash": c.content_hash,
                        "part": c.part,
                    },
                )
                for c, d, s in zip(part, dense, sparse, strict=True)
            ]
            await self.client.upsert(name, points=points, wait=True)
            done += len(part)
            if progress:
                progress(done, len(chunks))
        return done

    async def count(self, name: str) -> int:
        if not await self.client.collection_exists(name):
            return 0
        return (await self.client.count(name, exact=True)).count

    async def all_point_ids(self, name: str) -> set[str]:
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = await self.client.scroll(
                name, limit=512, offset=offset, with_payload=False
            )
            ids.update(str(p.id) for p in points)
            if offset is None:
                return ids

    async def search(
        self, name: str, query: str, k: int = 10, mode: SearchMode = "hybrid",
        kinds: list[str] | None = None, prefetch_k: int = 50,
    ) -> list[Hit]:  # fmt: skip
        dense, sparse = await asyncio.to_thread(self.embedder.embed_query, query)
        flt = (
            models.Filter(
                must=[models.FieldCondition(key="kind", match=models.MatchAny(any=kinds))]
            )
            if kinds
            else None
        )
        sparse_q = models.SparseVector(indices=sparse.indices, values=sparse.values)
        if mode == "dense":
            resp = await self.client.query_points(
                name, query=dense, using=DENSE, query_filter=flt, limit=k
            )
        elif mode == "sparse":
            if not sparse.indices:
                return []
            resp = await self.client.query_points(
                name, query=sparse_q, using=SPARSE, query_filter=flt, limit=k
            )
        else:
            prefetch = [models.Prefetch(query=dense, using=DENSE, filter=flt, limit=prefetch_k)]
            if sparse.indices:
                prefetch.append(
                    models.Prefetch(query=sparse_q, using=SPARSE, filter=flt, limit=prefetch_k)
                )
            resp = await self.client.query_points(
                name, prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF), limit=k
            )
        return [_hit(p) for p in resp.points]


def _hit(p: Any) -> Hit:
    pl = p.payload or {}
    return Hit(
        chunk_id=pl.get("chunk_id", ""), path=pl.get("path", ""), start_line=pl.get("start_line", 1),
        end_line=pl.get("end_line", 1), kind=pl.get("kind", ""), qualified_name=pl.get("qualified_name"),
        unit_kind=pl.get("unit_kind"), text=pl.get("text", ""), score=float(p.score or 0.0),
    )  # fmt: skip


def sparse_is_empty(v: SparseVec) -> bool:
    return not v.indices
