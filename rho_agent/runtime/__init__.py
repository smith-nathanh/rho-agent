"""Legacy runtime API — kept for deferred consumers (conductor).

These will be removed once conductor is migrated to the new core API.
Imports are lazy to avoid cascading failures from deleted modules.
"""

from __future__ import annotations


def __getattr__(name: str):  # noqa: N807
    """Lazy imports for legacy runtime symbols."""
    if name == "create_runtime":
        from .factory import create_runtime
        return create_runtime
    if name == "RuntimeOptions":
        from .options import RuntimeOptions
        return RuntimeOptions
    if name in ("run_prompt", "run_prompt_stored"):
        from .run import run_prompt, run_prompt_stored
        return run_prompt if name == "run_prompt" else run_prompt_stored
    if name in ("LocalRuntime", "RunResult", "RunState", "SessionUsage", "ToolApprovalItem", "session_usage"):
        from . import types as _types
        return getattr(_types, name)
    raise AttributeError(f"module 'rho_agent.runtime' has no attribute {name!r}")


__all__ = [
    "RuntimeOptions",
    "LocalRuntime",
    "RunResult",
    "RunState",
    "SessionUsage",
    "ToolApprovalItem",
    "create_runtime",
    "run_prompt",
    "run_prompt_stored",
    "session_usage",
]
