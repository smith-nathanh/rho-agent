"""Harbor/TerminalBench integration for ro-agent.

This module provides tools and wrappers for running ro-agent inside
Harbor's sandboxed container environments for TerminalBench evaluation.

Usage:
    # From within a Harbor container:
    python -m ro_agent.harbor.runner "task instruction here"

    # Or via Harbor job config:
    # agents:
    #   - import_path: ro_agent.harbor.agent:RoAgent
"""

from .tools import BashHandler, EditFileHandler, WriteFileHandler

__all__ = [
    "BashHandler",
    "EditFileHandler",
    "WriteFileHandler",
]
