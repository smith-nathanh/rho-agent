"""Harbor BaseAgent wrapper for rho-agent.

This module provides a Harbor-compatible agent that runs rho-agent
inside Harbor's container environment for TerminalBench evaluation.

Usage in job.yaml:
    agents:
      - import_path: rho_agent.eval.harbor.agent:RhoAgent
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from dotenv import load_dotenv
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Load .env from rho-agent project root (4 levels up from this file)
_pkg_root = Path(__file__).parent.parent.parent.parent
for _env_path in [_pkg_root / ".env", Path("/rho-agent/.env")]:
    if _env_path.exists():
        load_dotenv(_env_path)
        break


class RhoAgent(BaseAgent):
    """Runs rho-agent inside Harbor's container environment.

    This agent wrapper:
    1. Installs rho-agent in the container during setup
    2. Runs the rho-agent.harbor.runner module with the task instruction
    3. Returns results for Harbor's verification system

    The container provides sandboxing, so rho-agent uses unrestricted
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
        bash_only: bool = False,
        enable_reviewer: bool = False,
        reviewer_max_iterations: int = 1,
        *args,
        **kwargs,
    ) -> None:
        """Initialize the agent.

        Args:
            logs_dir: Directory to write agent logs to.
            model_name: Model to use (e.g., "openai/gpt-5-mini").
            logger: Logger instance.
            agent_timeout_sec: Maximum time for agent execution (default: 10 min).
            bash_only: If True, only provide bash tool (no Read, Grep, etc.).
            enable_reviewer: If True, run post-execution review after actor completes.
            reviewer_max_iterations: Max review-revise loops (0 = review only, no revision).
        """
        super().__init__(logs_dir, model_name, logger, *args, **kwargs)
        self._agent_timeout_sec = agent_timeout_sec
        self._bash_only = bash_only
        self._enable_reviewer = enable_reviewer
        self._reviewer_max_iterations = reviewer_max_iterations

    @staticmethod
    def name() -> str:
        """Return the agent name for Harbor."""
        return "rho-agent"

    def version(self) -> str | None:
        """Return the agent version."""
        # TODO: Read from pyproject.toml
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Install rho-agent in the container.

        Called by Harbor before running the agent on tasks.
        """
        # Log resolved model config so it's visible at launch
        model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or self.model_name or "gpt-5-mini"
        if "/" in model:
            model = model.split("/", 1)[1]
        base_url = os.environ.get("RHO_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        service_tier = os.environ.get("RHO_AGENT_SERVICE_TIER")
        self.logger.info(f"Model: {model} | Base URL: {base_url}" + (f" | Service tier: {service_tier}" if service_tier else ""))

        self.logger.info("Setting up rho-agent in container...")

        # Find rho-agent source directory (go up from this file)
        rho_agent_root = Path(__file__).parent.parent.parent.parent
        self.logger.info(f"Uploading rho-agent from {rho_agent_root}")

        # Upload rho-agent source to container
        await environment.upload_dir(rho_agent_root, "/rho-agent")

        # Install curl if needed (for uv installer)
        await environment.exec(
            "command -v curl || (apt-get update && apt-get install -y curl)",
            timeout_sec=60,
        )

        # Install uv if not available
        result = await environment.exec("command -v uv", timeout_sec=10)
        if result.return_code != 0:
            self.logger.info("Installing uv...")
            await environment.exec(
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
                timeout_sec=60,
            )

        # Sync rho-agent dependencies using uv (handles Python + deps automatically)
        self.logger.info("Syncing rho-agent dependencies...")
        result = await environment.exec(
            'export PATH="$HOME/.local/bin:$PATH" && cd /rho-agent && uv sync',
            timeout_sec=180,
        )
        self.logger.info(f"uv sync: rc={result.return_code}")
        if result.return_code != 0:
            self.logger.error(f"uv sync failed: {result.stdout}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run rho-agent on the task.

        Args:
            instruction: The task instruction from instruction.md.
            environment: Harbor's container environment for execution.
            context: Agent context for tracking tokens and trajectories.
        """
        # Escape instruction for shell
        escaped = shlex.quote(instruction)

        # Build environment variables
        # Strip provider prefix from model name (e.g., "openai/gpt-5-mini" -> "gpt-5-mini")
        # Env var overrides config so you can switch models without editing YAML
        model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or self.model_name or "gpt-5-mini"
        if "/" in model:
            model = model.split("/", 1)[1]

        telemetry_db = "/tmp/telemetry.db"
        env = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "RHO_AGENT_MODEL": model,
            "RHO_AGENT_TELEMETRY_DB": telemetry_db,
        }

        # Add base URL if configured
        base_url = os.environ.get("RHO_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if base_url:
            env["RHO_AGENT_BASE_URL"] = base_url

        # Add service tier if configured (e.g., "flex" for lower cost)
        service_tier = os.environ.get("RHO_AGENT_SERVICE_TIER")
        if service_tier:
            env["RHO_AGENT_SERVICE_TIER"] = service_tier

        # Add reviewer config if enabled
        if self._enable_reviewer:
            env["RHO_AGENT_ENABLE_REVIEWER"] = "1"
            env["RHO_AGENT_REVIEWER_MAX_ITERATIONS"] = str(self._reviewer_max_iterations)

        self.logger.info(
            f"Running rho-agent with model: {env['RHO_AGENT_MODEL']}, "
            f"bash_only: {self._bash_only}, "
            f"reviewer: {self._enable_reviewer}"
        )

        # Use longer timeout for flex tier (slower but cheaper)
        timeout = self._agent_timeout_sec
        if service_tier == "flex":
            timeout = max(timeout, 1800)  # at least 30 min for flex

        # Run rho-agent in the container using uv run
        # Tee stdout/stderr to files in the container so we can retrieve them
        # even if the command times out (environment.exec raises on timeout
        # before returning result, so we'd lose all output otherwise).
        bash_only_flag = " --bash-only" if self._bash_only else ""
        cmd = (
            f'set -a && source /rho-agent/.env && set +a && '
            f'export PATH="$HOME/.local/bin:$PATH" && '
            f'export PYTHONPATH=/rho-agent && '
            f'cd /app && '
            f'/rho-agent/.venv/bin/python -B -m rho_agent.eval.harbor.runner {escaped} /app{bash_only_flag} '
            f'> >(tee /tmp/agent_stdout.txt) 2> >(tee /tmp/agent_stderr.txt >&2)'
        )
        stdout = ""
        stderr = ""
        try:
            result = await environment.exec(
                f"bash -c {shlex.quote(cmd)}",
                timeout_sec=timeout,
                env=env,
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except Exception:
            # Timeout or other error - retrieve output from container files
            self.logger.warning("Agent exec failed, retrieving output from container...")
            try:
                out = await environment.exec("cat /tmp/agent_stdout.txt", timeout_sec=10)
                stdout = out.stdout or ""
            except Exception:
                stdout = ""
            try:
                err = await environment.exec("cat /tmp/agent_stderr.txt", timeout_sec=10)
                stderr = err.stdout or ""
            except Exception:
                stderr = ""
            raise  # Re-raise so Harbor records the timeout
        finally:
            # Log output
            if stdout:
                self.logger.info(f"stdout:\n{stdout}")
            if stderr:
                self.logger.warning(f"stderr:\n{stderr}")

            # Write output to logs directory for debugging
            if self.logs_dir:
                (self.logs_dir / "agent_stdout.txt").write_text(stdout)
                (self.logs_dir / "agent_stderr.txt").write_text(stderr)

            # Retrieve telemetry DB for full tool traces
            if self.logs_dir:
                try:
                    await environment.download_file(
                        telemetry_db, self.logs_dir / "telemetry.db"
                    )
                except Exception:
                    self.logger.debug("No telemetry DB to retrieve")


# For Harbor's import_path to work
__all__ = ["RhoAgent"]
