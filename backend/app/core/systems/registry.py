"""Map system names to implementations (all share the `System.answer` interface)."""

from __future__ import annotations

from app.core.services import Services
from app.core.systems.base import System


def get_system(name: str, svc: Services) -> System:
    if name == "naive_rag":
        from app.core.systems.naive_rag import NaiveRAG

        return NaiveRAG(svc)
    if name == "indexed_agent":
        from app.core.systems.indexed_agent import IndexedAgent

        return IndexedAgent(svc)
    if name == "grep_agent":
        from app.core.systems.grep_agent import GrepAgent

        return GrepAgent(svc)
    raise ValueError(f"Unknown system {name!r}")
