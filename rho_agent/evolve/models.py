"""Data models for the evolve module."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


@dataclass
class EvolveConfig:
    """Configuration for an evolution run."""

    harness: str  # dotted path to DomainHarness subclass
    run_dir: str = "./evolve-runs"
    model: str = field(default_factory=lambda: DEFAULT_MODEL)
    task_model: str | None = None
    max_generations: int = 20
    staged_sample_n: int = 3
    parallel: int = 1
    seed_workspace: str | None = None
    harness_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_task_model(self) -> str:
        return self.task_model or self.model


@dataclass
class Generation:
    """A single generation in the evolution archive."""

    gen_id: str  # e.g. "gen-0003-a1b2c3"
    generation: int  # ordinal
    parent_id: str | None
    workspace_path: str
    score: float | None = None
    staged_score: float | None = None
    status: str = "pending"  # pending | evaluating | scored | error | filtered
    error: str | None = None
    meta_usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""  # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Generation:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
