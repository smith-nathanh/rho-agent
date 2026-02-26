"""Continuum — continuity-first agent loop for PRD-to-code implementation."""

from .models import (
    DEFAULT_MODEL,
    ContinuumConfig,
    ContinuumState,
    SessionUsage,
    VerificationConfig,
)

__all__ = [
    "DEFAULT_MODEL",
    "ContinuumConfig",
    "ContinuumState",
    "SessionUsage",
    "VerificationConfig",
    "run_continuum",
]


def __getattr__(name: str):
    if name == "run_continuum":
        from .loop import run_continuum

        return run_continuum
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
