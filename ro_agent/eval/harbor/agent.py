"""Harbor BaseAgent wrapper for ro-agent.

This module provides a Harbor-compatible agent that runs ro-agent
inside Harbor's container environment for TerminalBench evaluation.

Usage in job.yaml:
    agents:
      - import_path: ro_agent.eval.harbor.agent:RoAgent
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

# Load .env from ro-agent project root (4 levels up from this file)
_pkg_root = Path(__file__).parent.parent.parent.parent
for _env_path in [_pkg_root / ".env", Path("/ro-agent/.env")]:
    if _env_path.exists():
        load_dotenv(_env_path)
        break


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
        # Log resolved model config so it's visible at launch
        model = os.environ.get("RO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or self.model_name or "gpt-5-mini"
        if "/" in model:
            model = model.split("/", 1)[1]
        base_url = os.environ.get("RO_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        service_tier = os.environ.get("RO_AGENT_SERVICE_TIER")
        self.logger.info(f"Model: {model} | Base URL: {base_url}" + (f" | Service tier: {service_tier}" if service_tier else ""))

        self.logger.info("Setting up ro-agent in container...")

        # Find ro-agent source directory (go up from this file)
        ro_agent_root = Path(__file__).parent.parent.parent.parent
        self.logger.info(f"Uploading ro-agent from {ro_agent_root}")

        # Upload ro-agent source to container
        await environment.upload_dir(ro_agent_root, "/ro-agent")

        # Verify upload
        result = await environment.exec("ls -la /ro-agent", timeout_sec=10)
        self.logger.info(f"Upload check: {result.stdout}")

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

        # Sync ro-agent dependencies using uv (handles Python + deps automatically)
        self.logger.info("Syncing ro-agent dependencies...")
        result = await environment.exec(
            'export PATH="$HOME/.local/bin:$PATH" && cd /ro-agent && uv sync',
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
        """Run ro-agent on the task.

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
        model = os.environ.get("RO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or self.model_name or "gpt-5-mini"
        if "/" in model:
            model = model.split("/", 1)[1]

        telemetry_db = "/tmp/telemetry.db"
        env = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "RO_AGENT_MODEL": model,
            "RO_AGENT_TELEMETRY_DB": telemetry_db,
        }

        # Add base URL if configured
        base_url = os.environ.get("RO_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if base_url:
            env["RO_AGENT_BASE_URL"] = base_url

        # Add service tier if configured (e.g., "flex" for lower cost)
        service_tier = os.environ.get("RO_AGENT_SERVICE_TIER")
        if service_tier:
            env["RO_AGENT_SERVICE_TIER"] = service_tier

        self.logger.info(f"Running ro-agent with model: {env['RO_AGENT_MODEL']}")

        # Use longer timeout for flex tier (slower but cheaper)
        timeout = self._agent_timeout_sec
        if service_tier == "flex":
            timeout = max(timeout, 1800)  # at least 30 min for flex

        # Run ro-agent in the container using uv run
        # Tee stdout/stderr to files in the container so we can retrieve them
        # even if the command times out (environment.exec raises on timeout
        # before returning result, so we'd lose all output otherwise).
        cmd = (
            f'set -a && source /ro-agent/.env && set +a && '
            f'export PATH="$HOME/.local/bin:$PATH" && '
            f'cd /app && '
            f'/ro-agent/.venv/bin/python -m ro_agent.eval.harbor.runner {escaped} /app '
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
__all__ = ["RoAgent"]
