"""Optional API dense embeddings (OpenAI); sparse BM25 vectors still come from fastembed."""

from __future__ import annotations

import openai

from app.core.index.embedder import SparseVec, _sparse


class OpenAIDenseEmbedder:
    def __init__(self, api_key: str | None, model: str, sparse_model: str, cache_dir: str) -> None:
        self.client = openai.OpenAI(api_key=api_key)
        self.dense_model = model
        self.sparse_model = sparse_model
        self.cache_dir = cache_dir
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_query("probe")[0])
        return self._dim

    def _dense(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(model=self.dense_model, input=texts)
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]

    def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], list[SparseVec]]:
        sparse = [
            SparseVec(s.indices.tolist(), s.values.tolist())
            for s in _sparse(self.sparse_model, self.cache_dir).embed(texts)
        ]
        return self._dense(texts), sparse

    def embed_query(self, text: str) -> tuple[list[float], SparseVec]:
        s = next(iter(_sparse(self.sparse_model, self.cache_dir).query_embed(text)))
        return self._dense([text])[0], SparseVec(s.indices.tolist(), s.values.tolist())
