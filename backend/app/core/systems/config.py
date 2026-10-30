"""Run configuration shared by all three systems, the API and the eval harness.

Every ablation is a flag here, so an eval run's saved config fully describes what ran.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings

SystemName = Literal["naive_rag", "indexed_agent", "grep_agent"]


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: SystemName = "indexed_agent"
    # Retrieval (naive_rag, indexed_agent)
    chunking: Literal["ast", "fixed"] = "ast"
    search_mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    rerank: bool = True
    rerank_candidates: int = Field(default=30, ge=1, le=200)
    top_k: int = Field(default=8, ge=1, le=50)
    # indexed_agent tool groups (ablations)
    graph_tools: bool = True
    history_tools: bool = True
    # grep_agent: optional GitHub PR lookup (the "fairer" comparison)
    grep_agent_github: bool = False
    # Shared budgets: identical across compared systems (None = take from Settings)
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    tool_output_max_chars: int | None = None
    answer_max_tokens: int = Field(default=4096, ge=256)
    # Model (None = Settings.llm_provider / llm_model)
    llm_provider: Literal["anthropic", "openai", "ollama"] | None = None
    llm_model: str | None = None
    # Citation verifier
    verify: bool = True
    revision_pass: bool = True

    def resolved(self, settings: Settings) -> SystemConfig:
        """Fill every None from Settings so saved configs are explicit."""
        return self.model_copy(
            update={
                "max_tool_calls": self.max_tool_calls or settings.agent_max_tool_calls,
                "max_tokens": self.max_tokens or settings.agent_max_tokens,
                "tool_output_max_chars": self.tool_output_max_chars
                or settings.tool_output_max_chars,
                "llm_provider": self.llm_provider or settings.llm_provider,
                "llm_model": self.llm_model or settings.llm_model,
            }
        )

    @classmethod
    def from_yaml(cls, path: Path, **overrides) -> SystemConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)
