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
    daytona_backend: Any = None  # DaytonaBackend | None
    parent_strategy: str = "tournament"
    meta_timeout: int = 3600  # seconds
    transfer_from: str | None = None  # path to a previous run dir for cross-run transfer

    @property
    def effective_task_model(self) -> str:
        return self.task_model or self.model

    def to_serializable_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict for persisting run config."""
        d = asdict(self)
        # daytona_backend is a dataclass or None — normalize to string
        if self.daytona_backend is not None:
            d["daytona_backend"] = "daytona"
        return d


@dataclass
class Generation:
    """A single generation in the evolution archive."""

    gen_id: str  # e.g. "gen-0003-a1b2c3"
    generation: int  # ordinal
    parent_id: str | None
    workspace_path: str
    diff_path: str | None = None  # path to .diff file (source of truth)
    score: float | None = None
    staged_score: float | None = None
    status: str = "pending"  # pending | evaluating | scored | error | filtered
    valid_parent: bool = True  # set false when meta-agent consistently fails from this node
    error: str | None = None
    meta_usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""  # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Generation:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
