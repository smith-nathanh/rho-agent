"""Harbor/TerminalBench integration for rho-agent.

This module provides the runner for executing rho-agent inside Harbor's
sandboxed container environments for TerminalBench evaluation.

Usage:
    # From within a Harbor container:
    python -m rho_agent.eval.harbor.runner "task instruction here"

    # Or via Harbor job config:
    # agents:
    #   - import_path: rho_agent.eval.harbor.agent:RhoAgent
"""

# Re-export handlers for convenience
from rho_agent.tools.handlers import BashHandler, WriteHandler, EditHandler

__all__ = [
    "BashHandler",
    "WriteHandler",
    "EditHandler",
]
