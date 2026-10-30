"""Process-wide service container: settings, SQLite, Qdrant, embedder, reranker, LLM factory."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import cached_property

from qdrant_client import AsyncQdrantClient

from app.config import Settings, get_settings
from app.core.index.embedder import Embedder, FastEmbedEmbedder, FastEmbedReranker, Reranker
from app.core.index.vector_store import VectorStore
from app.core.llm.base import LLMProvider, get_provider
from app.core.pipeline import RepoPaths
from app.core.repo_context import RepoContext
from app.db.connection import init_db


@dataclass
class Services:
    settings: Settings
    conn: sqlite3.Connection
    qdrant: AsyncQdrantClient
    embedder: Embedder
    reranker: Reranker | None
    provider_overrides: dict[str, LLMProvider] = field(default_factory=dict)  # tests inject fakes

    @cached_property
    def store(self) -> VectorStore:
        return VectorStore(self.qdrant, self.embedder)

    def provider(self, name: str | None = None, model: str | None = None) -> LLMProvider:
        key = f"{name or self.settings.llm_provider}:{model or self.settings.llm_model}"
        if key in self.provider_overrides:
            return self.provider_overrides[key]
        if "*" in self.provider_overrides:
            return self.provider_overrides["*"]
        return get_provider(self.settings, model=model, provider=name)

    def paths(self, repo_id: str) -> RepoPaths:
        return RepoPaths.for_repo(self.settings, repo_id)

    def repo_context(self, repo_id: str) -> RepoContext:
        p = self.paths(repo_id)
        return RepoContext(repo_id=repo_id, checkout=p.checkout, conn=self.conn, graph_path=p.graph)


def make_qdrant(settings: Settings) -> AsyncQdrantClient:
    if settings.qdrant_url == ":memory:":
        return AsyncQdrantClient(location=":memory:")
    key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    return AsyncQdrantClient(url=settings.qdrant_url, api_key=key, timeout=60)


def make_embedder(settings: Settings) -> Embedder:
    if settings.dense_embedding_provider == "openai":
        from app.core.index.openai_embedder import OpenAIDenseEmbedder

        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        return OpenAIDenseEmbedder(key, settings.dense_embedding_model, settings.sparse_embedding_model,
                                   str(settings.models_dir))  # fmt: skip
    return FastEmbedEmbedder(
        settings.dense_embedding_model, settings.sparse_embedding_model, settings.models_dir
    )


def build_services(settings: Settings | None = None, **overrides) -> Services:
    settings = settings or get_settings()
    conn = overrides.pop("conn", None) or init_db(settings.db_path)
    return Services(
        settings=settings,
        conn=conn,
        qdrant=overrides.pop("qdrant", None) or make_qdrant(settings),
        embedder=overrides.pop("embedder", None) or make_embedder(settings),
        reranker=overrides.pop("reranker", None)
        or FastEmbedReranker(settings.reranker_model, settings.models_dir),
        **overrides,
    )
