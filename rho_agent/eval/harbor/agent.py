"""Harbor installed-agent adapter for rho-agent.

Harbor installs rho-agent inside the task container and runs
``rho_agent.eval.harbor.runner`` there. Written against Harbor 0.25.

Example::

    harbor run -d terminal-bench/terminal-bench@4.1.0 \\
        -a rho_agent.eval.harbor.agent:RhoAgent -m openai/gpt-6.1-sol \\
        --ak install_source=git --ak version=<branch-or-sha> \\
        --ak bash_only=true --ak cost_ceiling_usd=4
"""

from __future__ import annotations

import json
import shlex
from importlib import metadata
from pathlib import PurePosixPath
from typing import Literal, override

from harbor.agents.agent_auth_resolution import ModelConnectionSpec, ProviderAccess
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.llms.litellm_config import (
    LITELLM_BASE_URL_OVERRIDES,
    litellm_model_name,
    require_litellm_credentials,
)
from harbor.models.agent.context import AgentContext
from harbor.models.model_api_format import ModelApiFormat
from pydantic import Field

DEFAULT_REPO_URL = "https://github.com/smith-nathanh/rho-agent.git"
# Python for the agent's own virtualenv, independent of the task image's Python.
AGENT_PYTHON_VERSION = "3.12"
# Under the agent user's home so installation needs no root.
VENV_DIR = "$HOME/.rho-agent/venv"

_UV_ON_PATH = (
    'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
    'else export PATH="$HOME/.local/bin:$PATH"; fi'
)


class RhoAgentOptions(InstalledAgentOptions):
    """Options accepted as ``--ak key=value`` or ``kwargs`` in a job config.

    ``version`` (inherited) is the PyPI version, or the git branch, tag or
    commit when ``install_source`` is ``git``. ``reasoning_effort`` is also
    inherited and is set with Harbor's ``--effort``.
    """

    install_source: Literal["pypi", "git"] = Field(
        default="pypi", description="Install rho-agent from PyPI or from a git repository."
    )
    repo_url: str = Field(
        default=DEFAULT_REPO_URL, description="Repository used when install_source is git."
    )
    bash_only: bool = Field(
        default=False, description="Give the agent only the bash tool (no read, grep, edit)."
    )
    enable_confirm_done: bool = Field(
        default=True,
        description="Ask the agent to verify its work before it is allowed to finish.",
    )
    confirm_done_max: int = Field(
        default=3, ge=0, description="Maximum verification rounds before finishing anyway."
    )
    enable_reviewer: bool = Field(
        default=False, description="Run a separate review pass after the agent finishes."
    )
    reviewer_max_iterations: int = Field(
        default=1, ge=0, description="Maximum review-and-revise loops."
    )
    temperature: float | None = Field(
        default=None, description="Sampling temperature. Omit for the provider default."
    )
    cost_ceiling_usd: float = Field(
        default=0.0, ge=0, description="Stop the agent at this cost per task. 0 disables."
    )
    max_wall_seconds: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Stop the agent after this many seconds so it exits cleanly and is graded. "
            "Set it below the task's agent timeout. 0 disables."
        ),
    )
    max_call_seconds: float = Field(
        default=1200.0,
        ge=0,
        description="Abandon and retry a single model call after this long. 0 disables.",
    )


class RhoAgent(BaseInstalledAgent):
    """Runs rho-agent inside Harbor's task environment."""

    options_model = RhoAgentOptions
    options: RhoAgentOptions

    capabilities = AgentCapabilities(base_url=True, reasoning_effort=True, atif=True)

    # The runner calls models through LiteLLM, so credentials and endpoints are
    # resolved the way Harbor resolves them for its other LiteLLM-based agents.
    MODEL_CONNECTION = ModelConnectionSpec(
        base_url_overrides=LITELLM_BASE_URL_OVERRIDES,
        supported_api_formats=[
            ModelApiFormat.OPENAI_CHAT_COMPLETIONS,
            ModelApiFormat.ANTHROPIC_MESSAGES,
        ],
        agent_provider_access=ProviderAccess(
            api_key_candidate_names=["RHO_AGENT_API_KEY"],
            base_url_candidate_names=["RHO_AGENT_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"],
        ),
        base_url_destination_names={
            "default": "OPENAI_BASE_URL",
            ModelApiFormat.ANTHROPIC_MESSAGES: "ANTHROPIC_API_BASE",
        },
    )

    @classmethod
    @override
    def check_model_credentials(cls, connection, *, kwargs, env, explicit_env=None):
        require_litellm_credentials(connection, cls.name())

    @staticmethod
    @override
    def name() -> str:
        return "rho-agent"

    @override
    def version(self) -> str | None:
        if self._version:
            return self._version
        try:
            return metadata.version("rho-agent")
        except metadata.PackageNotFoundError:
            return None

    @override
    def get_version_command(self) -> str:
        return (
            f"{VENV_DIR}/bin/python -c "
            "\"import importlib.metadata; print(importlib.metadata.version('rho-agent'))\""
        )

    # --- install ---

    def _package_spec(self) -> str:
        """The requirement passed to ``uv pip install``."""
        if self.options.install_source == "git":
            ref = f"@{self._version}" if self._version else ""
            return f"rho-agent[evals] @ git+{self.options.repo_url}{ref}"
        pin = f"=={self._version}" if self._version else ""
        return f"rho-agent[evals]{pin}"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "bash", "git"))
        python_version = shlex.quote(AGENT_PYTHON_VERSION)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ! command -v uv >/dev/null 2>&1; then"
                "  curl -LsSf https://astral.sh/uv/install.sh | sh;"
                " fi && "
                f"{_UV_ON_PATH} && "
                # A private Python keeps the agent independent of the task
                # image, which may ship an old interpreter or none at all.
                f"uv python install {python_version} && "
                f'rm -rf "{VENV_DIR}" && '
                f'uv venv --python {python_version} "{VENV_DIR}" && '
                f'uv pip install --python "{VENV_DIR}/bin/python" '
                f"{shlex.quote(self._package_spec())} && "
                f'"{VENV_DIR}/bin/python" -c "import rho_agent.eval.harbor.runner"'
            ),
        )

    # --- run ---

    def _runner_env(self) -> dict[str, str]:
        """Environment for the runner process: credentials plus runner settings."""
        if not self.model_name:
            raise ValueError("A model is required, in the form provider/model_name")

        access = self.model_connection
        require_litellm_credentials(access, self.name())
        opts = self.options
        env: dict[str, str] = {
            **access.env,
            "RHO_AGENT_MODEL": litellm_model_name(
                self.model_name,
                model_api_format=self._model_api_format,
                skip_model_parsing=self.skip_model_parsing,
            ),
            "RHO_AGENT_LOGS_DIR": str(self.environment_logs_dir),
            "RHO_AGENT_CONFIRM_DONE": "1" if opts.enable_confirm_done else "0",
            "RHO_AGENT_CONFIRM_DONE_MAX": str(opts.confirm_done_max),
            "RHO_AGENT_MAX_CALL_SECONDS": str(opts.max_call_seconds),
        }
        if opts.enable_reviewer:
            env["RHO_AGENT_ENABLE_REVIEWER"] = "1"
            env["RHO_AGENT_REVIEWER_MAX_ITERATIONS"] = str(opts.reviewer_max_iterations)
        if opts.temperature is not None:
            env["RHO_AGENT_TEMPERATURE"] = str(opts.temperature)
        if opts.reasoning_effort:
            env["RHO_AGENT_REASONING_EFFORT"] = opts.reasoning_effort
        if opts.cost_ceiling_usd > 0:
            env["RHO_AGENT_COST_CEILING_USD"] = str(opts.cost_ceiling_usd)
        if opts.max_wall_seconds > 0:
            env["RHO_AGENT_MAX_WALL_SECONDS"] = str(opts.max_wall_seconds)
        return env

    def _run_command(self, instruction: str) -> str:
        logs = PurePosixPath(self.environment_logs_dir)
        flags = " --bash-only" if self.options.bash_only else ""
        return (
            f"mkdir -p {shlex.quote(str(logs))} && "
            f'"{VENV_DIR}/bin/python" -B -m rho_agent.eval.harbor.runner '
            f'{shlex.quote(instruction)} "$(pwd)"{flags} '
            f"2>&1 </dev/null | tee {shlex.quote(str(logs / 'stdout.txt'))}"
        )

    @with_prompt_template
    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        env = self._runner_env()
        self.logger.info(
            "Running rho-agent: model=%s bash_only=%s confirm_done=%s reviewer=%s "
            "effort=%s cost_ceiling_usd=%s max_wall_seconds=%s",
            env["RHO_AGENT_MODEL"],
            self.options.bash_only,
            self.options.enable_confirm_done,
            self.options.enable_reviewer,
            self.options.reasoning_effort,
            self.options.cost_ceiling_usd,
            self.options.max_wall_seconds,
        )
        await self.exec_as_agent(environment, command=self._run_command(instruction), env=env)

    # --- results ---

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill token usage and cost from the logs the runner wrote.

        ``tokens.json`` is rewritten after every tool call. If it is missing or
        unreadable, usage is rebuilt from the append-only ``trace.jsonl``, which
        survives a killed process.
        """
        tokens_path = self.logs_dir / "tokens.json"
        if tokens_path.exists():
            try:
                data = json.loads(tokens_path.read_text())
                context.n_input_tokens = data.get("input", 0)
                context.n_output_tokens = data.get("output", 0)
                context.n_cache_tokens = data.get("cached", 0)
                if "cost_usd" in data:
                    context.cost_usd = data["cost_usd"]
                return
            except Exception as e:
                self.logger.warning(f"Failed to parse tokens.json: {e}")

        trace_path = self.logs_dir / "trace.jsonl"
        if not trace_path.exists():
            return
        try:
            from rho_agent.core.state import State

            usage = State.from_jsonl(trace_path.read_bytes()).usage
            context.n_input_tokens = int(usage["input_tokens"])
            context.n_output_tokens = int(usage["output_tokens"])
            context.n_cache_tokens = int(usage["cached_tokens"])
            context.cost_usd = float(usage["cost_usd"])
        except Exception as e:
            self.logger.warning(f"Failed to rebuild usage from trace.jsonl: {e}")


__all__ = ["RhoAgent", "RhoAgentOptions"]
