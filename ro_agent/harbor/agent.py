"""Harbor BaseAgent wrapper for ro-agent.

This module provides a Harbor-compatible agent that runs ro-agent
inside Harbor's container environment for TerminalBench evaluation.

Usage in job.yaml:
    agents:
      - import_path: ro_agent.harbor.agent:RoAgent
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class RoAgent(BaseAgent):
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

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        agent_timeout_sec: int = 600,
        *args,
        **kwargs,
    ) -> None:
        """Initialize the agent.

        Args:
            logs_dir: Directory to write agent logs to.
            model_name: Model to use (e.g., "openai/gpt-5-mini").
            logger: Logger instance.
            agent_timeout_sec: Maximum time for agent execution (default: 10 min).
        """
        super().__init__(logs_dir, model_name, logger, *args, **kwargs)
        self._agent_timeout_sec = agent_timeout_sec

    @staticmethod
    def name() -> str:
        """Return the agent name for Harbor."""
        return "ro-agent"

    def version(self) -> str | None:
        """Return the agent version."""
        # TODO: Read from pyproject.toml
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Install ro-agent in the container.

        Called by Harbor before running the agent on tasks.
        """
        self.logger.info("Setting up ro-agent in container...")

        # Install ro-agent from the mounted source directory
        # The Harbor job config should mount the ro-agent source at /ro-agent
        result = await environment.exec(
            "pip install -e /ro-agent",
            timeout_sec=120,
        )

        if result.returncode != 0:
            self.logger.warning(
                f"Failed to install from /ro-agent: {result.stderr}. "
                "Trying current directory..."
            )
            # Fallback: try installing from current directory
            result = await environment.exec(
                "pip install -e .",
                timeout_sec=120,
            )

        if result.returncode == 0:
            self.logger.info("ro-agent installed successfully")
        else:
            self.logger.error(f"Failed to install ro-agent: {result.stderr}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run ro-agent on the task.

        Args:
            instruction: The task instruction from instruction.md.
            environment: Harbor's container environment for execution.
            context: Agent context for tracking tokens and trajectories.
        """
        # Escape instruction for shell
        escaped = shlex.quote(instruction)

        # Build environment variables
        env = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "RO_AGENT_MODEL": self.model_name or os.environ.get("RO_AGENT_MODEL", "gpt-5-mini"),
            "RO_AGENT_MAX_TURNS": os.environ.get("RO_AGENT_MAX_TURNS", "50"),
        }

        # Add base URL if configured
        base_url = os.environ.get("RO_AGENT_BASE_URL")
        if base_url:
            env["RO_AGENT_BASE_URL"] = base_url

        self.logger.info(f"Running ro-agent with model: {env['RO_AGENT_MODEL']}")

        # Run ro-agent in the container
        result = await environment.exec(
            f"python -m ro_agent.harbor.runner {escaped}",
            cwd="/app",
            timeout_sec=self._agent_timeout_sec,
            env=env,
        )

        # Log output
        if result.stdout:
            self.logger.info(f"stdout:\n{result.stdout}")
        if result.stderr:
            self.logger.warning(f"stderr:\n{result.stderr}")

        # Write output to logs directory for debugging
        if self.logs_dir:
            (self.logs_dir / "agent_stdout.txt").write_text(result.stdout or "")
            (self.logs_dir / "agent_stderr.txt").write_text(result.stderr or "")

        # TODO: Parse token counts from runner output and populate context
        # The runner outputs "[Tokens: in=X, out=Y]" which we could parse
        # context.n_input_tokens = ...
        # context.n_output_tokens = ...


# For Harbor's import_path to work
__all__ = ["RoAgent"]
