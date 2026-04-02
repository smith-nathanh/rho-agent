"""Evolve — evolutionary loop for building and improving task-agents."""

from .harness import DomainHarness
from .models import EvolveConfig, Generation

__all__ = [
    "DomainHarness",
    "EvolveConfig",
    "Generation",
    "run_evolve",
]


def __getattr__(name: str):
    if name == "run_evolve":
        from .loop import run_evolve

        return run_evolve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
