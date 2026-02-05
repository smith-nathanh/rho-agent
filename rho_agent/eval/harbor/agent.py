"""Harbor BaseInstalledAgent wrapper for rho-agent.

This module provides a Harbor-compatible agent that runs rho-agent
inside Harbor's container environment for TerminalBench evaluation.

Usage in job.yaml:
    agents:
      - import_path: rho_agent.eval.harbor.agent:RhoAgent
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext


class RhoAgent(BaseInstalledAgent):
    """Runs rho-agent inside Harbor's container environment.

    This agent wrapper:
    1. Installs rho-agent from git in the container during setup
    2. Runs the rho_agent.eval.harbor.runner module with the task instruction
    3. Returns results for Harbor's verification system

    The container provides sandboxing, so rho-agent uses unrestricted
    eval-mode tools depending on the config settings.
    """

    # Harbor agent interface
    SUPPORTS_ATIF: bool = True

    RHO_AGENT_REPO = "https://github.com/smith-nathanh/rho-agent.git"

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
        super().__init__(logs_dir, model_name=model_name, logger=logger, *args, **kwargs)
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
        return self._version or "latest"

    @property
    def _install_agent_template_path(self) -> Path:
        """Path to the Jinja2 install script template."""
        return Path(__file__).parent / "install-rho-agent.sh.j2"

    @property
    def _template_variables(self) -> dict[str, str]:
        """Variables to pass to the install script template."""
        variables = {"repo_url": self.RHO_AGENT_REPO}
        if self._version:
            variables["version"] = self._version
        return variables

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Parse token usage and cost from telemetry, populate Harbor's AgentContext."""
        # Try incremental tokens file first (survives process kill)
        tokens_path = self.logs_dir / "tokens.json"
        if tokens_path.exists():
            try:
                data = json.loads(tokens_path.read_text())
                context.n_input_tokens = data.get("input", 0)
                context.n_output_tokens = data.get("output", 0)
                context.n_cache_tokens = data.get("cached", 0)
                if "cost_usd" in data:
                    context.cost_usd = data["cost_usd"]
                reasoning_tokens = data.get("reasoning", 0)
                self.logger.info(
                    f"Token usage: input={context.n_input_tokens}, "
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
                    f"Token usage (from DB): input={context.n_input_tokens}, "
                    f"output={context.n_output_tokens}"
                )
        except Exception as e:
            self.logger.warning(f"Failed to parse telemetry DB: {e}")

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        """Create commands to run rho-agent on the task."""
        # Build environment variables
        # Strip provider prefix from model name (e.g., "openai/gpt-5-mini" -> "gpt-5-mini")
        model = os.environ.get("RHO_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or self.model_name or "gpt-5-mini"
        if "/" in model:
            model = model.split("/", 1)[1]

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
            f"Running rho-agent with model: {model}, "
            f"bash_only: {self._bash_only}, "
            f"reviewer: {self._enable_reviewer}, "
            f"confirm_done: {self._enable_confirm_done}, "
            f"temperature: {self._temperature}, "
            f"reasoning_effort: {self._reasoning_effort}, "
            f"cost_ceiling_usd: {self._cost_ceiling_usd}"
        )

        # Escape instruction for shell
        escaped = shlex.quote(instruction)

        # Build the run command
        # Source .env for any additional config, then run the runner module
        # Use tee to stream output to mounted logs (survives timeout)
        # Note: .env may not exist (gitignored), so use || true to prevent chain failure
        bash_only_flag = " --bash-only" if self._bash_only else ""
        cmd = (
            f'set -a; [ -f /rho-agent/.env ] && source /rho-agent/.env || true; set +a; '
            f'export PATH="$HOME/.local/bin:$PATH"; '
            f'/rho-agent/.venv/bin/python -B -m rho_agent.eval.harbor.runner {escaped} "$(pwd)"{bash_only_flag} '
            f'2>&1 | tee /logs/agent/stdout.txt'
        )

        return [
            ExecInput(
                command=f"bash -c {shlex.quote(cmd)}",
                env=env,
            )
        ]


# For Harbor's import_path to work
__all__ = ["RhoAgent"]
