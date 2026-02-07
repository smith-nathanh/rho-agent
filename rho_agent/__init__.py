"""rho-agent: An agent harness with first-class CLI and programmatic APIs."""

__version__ = "0.1.0"

from .runtime import (
    AgentRuntime,
    AgentHandle,
    CancellationToken,
    RunResult,
    RuntimeOptions,
    create_runtime,
    dispatch_prompt,
    run_prompt,
)

__all__ = [
    "__version__",
    "RuntimeOptions",
    "AgentRuntime",
    "AgentHandle",
    "RunResult",
    "CancellationToken",
    "create_runtime",
    "run_prompt",
    "dispatch_prompt",
]
