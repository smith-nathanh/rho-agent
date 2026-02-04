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
    eval-mode tools depending on the config settings.
    """

    # Harbor agent interface
    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        bash_only: bool = False,
        enable_reviewer: bool = False,
        reviewer_max_iterations: int = 1,
        enable_confirm_done: bool = True,
        confirm_done_max: int = 3,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        cost_ceiling_usd: float = 0.0,
        *args,
        **kwargs,
    ) -> None:
        """Initialize the agent.

        Args:
            logs_dir: Directory to write agent logs to.
            model_name: Model to use (e.g., "openai/gpt-5-mini").
            logger: Logger instance.
            bash_only: If True, only provide bash tool (no Read, Grep, etc.).
            enable_reviewer: If True, run post-execution review after actor completes.
            reviewer_max_iterations: Max review-revise loops (0 = review only, no revision).
            enable_confirm_done: If True, require explicit CONFIRM_DONE after actor completes.
            confirm_done_max: Max confirm retries before proceeding (default: 3).
            temperature: Model temperature (default: None, uses API default).
            reasoning_effort: Reasoning effort level: "low", "medium", "high" (default: None).
            cost_ceiling_usd: Max cost per task in USD, 0 = disabled (default: 0.0).
        """
        super().__init__(logs_dir, model_name, logger, *args, **kwargs)
        self._bash_only = bash_only
        self._enable_reviewer = enable_reviewer
        self._reviewer_max_iterations = reviewer_max_iterations
        self._enable_confirm_done = enable_confirm_done
        self._confirm_done_max = confirm_done_max
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._cost_ceiling_usd = cost_ceiling_usd

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
        # Include evals extra for LiteLLM support
        self.logger.info("Syncing rho-agent dependencies...")
        result = await environment.exec(
            'export PATH="$HOME/.local/bin:$PATH" && cd /rho-agent && uv sync --extra evals',
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

        # Use /logs/agent/ which is mounted from host - no download needed
        env = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "RHO_AGENT_MODEL": model,
            "RHO_AGENT_TELEMETRY_DB": "/logs/agent/telemetry.db",
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

        # Add completion confirmation config
        env["RHO_AGENT_CONFIRM_DONE"] = "1" if self._enable_confirm_done else "0"
        env["RHO_AGENT_CONFIRM_DONE_MAX"] = str(self._confirm_done_max)

        # Add temperature config (only if explicitly set)
        if self._temperature is not None:
            env["RHO_AGENT_TEMPERATURE"] = str(self._temperature)

        # Add reasoning effort config (only if explicitly set)
        if self._reasoning_effort:
            env["RHO_AGENT_REASONING_EFFORT"] = self._reasoning_effort

        # Add cost ceiling config (only if set > 0)
        if self._cost_ceiling_usd > 0:
            env["RHO_AGENT_COST_CEILING_USD"] = str(self._cost_ceiling_usd)

        self.logger.info(
            f"Running rho-agent with model: {env['RHO_AGENT_MODEL']}, "
            f"bash_only: {self._bash_only}, "
            f"reviewer: {self._enable_reviewer}, "
            f"confirm_done: {self._enable_confirm_done}, "
            f"temperature: {self._temperature}, "
            f"reasoning_effort: {self._reasoning_effort}, "
            f"cost_ceiling_usd: {self._cost_ceiling_usd}"
        )

        # Run rho-agent in the container using uv run
        # Tee stdout/stderr to /logs/agent/ which is mounted from host
        # This ensures output survives even if the process is killed by timeout
        # Use the container's WORKDIR (which varies by task)
        bash_only_flag = " --bash-only" if self._bash_only else ""
        cmd = (
            f'set -a && source /rho-agent/.env && set +a && '
            f'export PATH="$HOME/.local/bin:$PATH" && '
            f'export PYTHONPATH=/rho-agent && '
            f'/rho-agent/.venv/bin/python -B -m rho_agent.eval.harbor.runner {escaped} "$(pwd)"{bash_only_flag} '
            f'> >(tee /logs/agent/stdout.txt) 2> >(tee /logs/agent/stderr.txt >&2)'
        )
        stdout = ""
        stderr = ""
        try:
            result = await environment.exec(
                f"bash -c {shlex.quote(cmd)}",
                env=env,
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except Exception:
            # Timeout or other error - output is already in /logs/agent/ via mount
            self.logger.warning("Agent exec failed (timeout or error)")
            raise  # Re-raise so Harbor records the timeout
        finally:
            # Read output from mounted logs directory (no download needed)
            if self.logs_dir:
                stdout_path = self.logs_dir / "stdout.txt"
                stderr_path = self.logs_dir / "stderr.txt"
                if stdout_path.exists():
                    stdout = stdout_path.read_text()
                if stderr_path.exists():
                    stderr = stderr_path.read_text()

            # Log output
            if stdout:
                self.logger.info(f"stdout:\n{stdout}")
            if stderr:
                self.logger.warning(f"stderr:\n{stderr}")

            # Populate Harbor context with token usage
            self._populate_context_from_telemetry(context)

    def _populate_context_from_telemetry(self, context: AgentContext) -> None:
        """Parse token usage, cost, and populate Harbor's AgentContext."""
        if not self.logs_dir:
            return

        # Try incremental tokens file first (survives process kill)
        tokens_path = self.logs_dir / "tokens.json"
        if tokens_path.exists():
            try:
                import json
                data = json.loads(tokens_path.read_text())
                context.n_input_tokens = data.get("input", 0)
                context.n_output_tokens = data.get("output", 0)
                reasoning_tokens = data.get("reasoning", 0)
                if "cost_usd" in data:
                    context.cost_usd = data["cost_usd"]
                self.logger.info(
                    f"Token usage (from tokens.json): input={context.n_input_tokens}, "
                    f"output={context.n_output_tokens}, reasoning={reasoning_tokens}, "
                    f"cost=${context.cost_usd or 0:.4f}"
                )
                return
            except Exception as e:
                self.logger.warning(f"Failed to parse tokens.json: {e}")

        # Fall back to telemetry DB
        telemetry_path = self.logs_dir / "telemetry.db"
        if not telemetry_path.exists():
            return

        try:
            import sqlite3
            conn = sqlite3.connect(telemetry_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT total_input_tokens, total_output_tokens FROM sessions LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()

            if row:
                context.n_input_tokens = row[0] or 0
                context.n_output_tokens = row[1] or 0
                self.logger.info(
                    f"Token usage (from DB): input={context.n_input_tokens}, output={context.n_output_tokens}"
                )
        except Exception as e:
            self.logger.warning(f"Failed to parse telemetry DB: {e}")


# For Harbor's import_path to work
__all__ = ["RhoAgent"]
