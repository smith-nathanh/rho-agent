"""rho-agent: An agent harness with first-class CLI and programmatic APIs."""

__version__ = "0.1.0"

from .runtime import (
    AgentRuntime,
    AgentHandle,
    CancellationToken,
    RunResult,
    RunState,
    RuntimeOptions,
    SqliteRunStore,
    ToolApprovalItem,
    close_runtime,
    create_runtime,
    dispatch_prompt,
    run_prompt,
    start_runtime,
)

__all__ = [
    "__version__",
    "RuntimeOptions",
    "AgentRuntime",
    "AgentHandle",
    "RunResult",
    "RunState",
    "ToolApprovalItem",
    "SqliteRunStore",
    "CancellationToken",
    "create_runtime",
    "start_runtime",
    "close_runtime",
    "run_prompt",
    "dispatch_prompt",
]
