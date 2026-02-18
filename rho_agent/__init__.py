"""rho-agent: An agent harness with first-class CLI and programmatic APIs."""

__version__ = "0.1.0"

from .runtime import (
    AgentHandle,
    CancellationToken,
    LocalRuntime,
    RunResult,
    RunState,
    Runtime,
    RuntimeOptions,
    SessionUsage,
    SqliteRunStore,
    ToolApprovalItem,
    create_runtime,
    dispatch_prompt,
    run_prompt,
    session_usage,
)

__all__ = [
    "__version__",
    "RuntimeOptions",
    "Runtime",
    "LocalRuntime",
    "AgentHandle",
    "RunResult",
    "RunState",
    "SessionUsage",
    "ToolApprovalItem",
    "SqliteRunStore",
    "CancellationToken",
    "create_runtime",
    "run_prompt",
    "dispatch_prompt",
    "session_usage",
]
