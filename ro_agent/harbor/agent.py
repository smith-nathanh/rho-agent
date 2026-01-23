"""Harbor BaseAgent wrapper for ro-agent.

This module provides a Harbor-compatible agent that runs ro-agent
inside Harbor's container environment for TerminalBench evaluation.

Usage in job.yaml:
    agents:
      - import_path: ro_agent.harbor.agent:RoAgent
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext


class RoAgent:
    """Runs ro-agent inside Harbor's container environment.

    This agent wrapper:
    1. Installs ro-agent in the container during setup
    2. Runs the ro-agent.harbor.runner module with the task instruction
    3. Returns results for Harbor's verification system

    The container provides sandboxing, so ro-agent uses unrestricted
    eval-mode tools (bash, write_file, edit_file).
    """

    # Harbor agent interface
    SUPPORTS_ATIF: bool = False  # TODO: Add trajectory support later

    def __init__(self, agent_timeout_sec: int = 600) -> None:
        """Initialize the agent.

        Args:
            agent_timeout_sec: Maximum time for agent execution (default: 10 min).
        """
        self._agent_timeout_sec = agent_timeout_sec

    @staticmethod
    def name() -> str:
        """Return the agent name for Harbor."""
        return "ro-agent"

    def version(self) -> str | None:
        """Return the agent version."""
        # TODO: Read from pyproject.toml
        return "0.1.0"

    async def setup(self, environment: "BaseEnvironment") -> None:
        """Install ro-agent in the container.

        Called by Harbor before running the agent on tasks.
        """
        # Option 1: Install from PyPI (when published)
        # await environment.exec("pip install ro-agent")

        # Option 2: Install from local copy mounted in container
        # The Harbor job config should mount the ro-agent source at /ro-agent
        result = await environment.exec(
            "pip install -e /ro-agent",
            timeout_sec=120,
        )

        if result.returncode != 0:
            # Fallback: try installing from current directory
            result = await environment.exec(
                "pip install -e .",
                timeout_sec=120,
            )

    async def run(
        self,
        instruction: str,
        environment: "BaseEnvironment",
        context: "AgentContext",
    ) -> None:
        """Run ro-agent on the task.

        Args:
            instruction: The task instruction from instruction.md.
            environment: Harbor's container environment for execution.
            context: Agent context for tracking tokens and trajectories.
        """
        # Escape instruction for shell
        escaped = shlex.quote(instruction)

        # Run ro-agent in the container
        result = await environment.exec(
            f"python -m ro_agent.harbor.runner {escaped}",
            cwd="/app",
            timeout_sec=self._agent_timeout_sec,
            env={
                # Pass through API configuration
                "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                "RO_AGENT_MODEL": "${RO_AGENT_MODEL:-gpt-5-mini}",
                "RO_AGENT_BASE_URL": "${RO_AGENT_BASE_URL:-}",
                "RO_AGENT_MAX_TURNS": "${RO_AGENT_MAX_TURNS:-50}",
            },
        )

        # Log output for debugging
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)

        # TODO: Parse token counts from runner output and populate context
        # context.n_input_tokens = ...
        # context.n_output_tokens = ...


# For Harbor's import_path to work
__all__ = ["RoAgent"]
