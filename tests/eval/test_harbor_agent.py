"""Tests for the Harbor adapter, run against the installed Harbor package."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("harbor", reason="harbor extra not installed")

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.models.agent.context import AgentContext

from rho_agent.eval.harbor.agent import RhoAgent


@dataclass
class _Result:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeEnvironment:
    """Records exec calls; every command succeeds."""

    default_user = None

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def exec(self, command: str, **kwargs: Any) -> _Result:
        self.calls.append({"command": command, **kwargs})
        return _Result()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    for name in ("RHO_AGENT_API_KEY", "RHO_AGENT_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)


def _agent(tmp_path: Path, **kwargs: Any) -> RhoAgent:
    kwargs.setdefault("model_name", "openai/gpt-6.1-sol")
    return RhoAgent(tmp_path, **kwargs)


def test_adapter_implements_the_current_harbor_interface(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    assert isinstance(agent, BaseInstalledAgent)
    assert RhoAgent.name() == "rho-agent"
    assert RhoAgent.capabilities.atif and RhoAgent.capabilities.reasoning_effort
    assert RhoAgent.import_path() == "rho_agent.eval.harbor.agent:RhoAgent"
    assert agent.to_agent_info().model_info.name == "gpt-6.1-sol"
    assert "cost_ceiling_usd" in json.dumps(RhoAgent.options_schema())


def test_options_are_validated_and_coerced_from_strings(tmp_path: Path) -> None:
    agent = _agent(tmp_path, bash_only="true", cost_ceiling_usd="4", confirm_done_max="2")
    assert agent.options.bash_only is True
    assert agent.options.cost_ceiling_usd == 4.0
    assert agent.options.confirm_done_max == 2

    with pytest.raises(ValueError, match="Unknown option 'bogus'"):
        _agent(tmp_path, bogus=1)
    with pytest.raises(ValueError, match="install_source"):
        _agent(tmp_path, install_source="tarball")


def test_package_spec_for_pypi_and_git(tmp_path: Path) -> None:
    assert _agent(tmp_path)._package_spec() == "rho-agent[evals]"
    assert _agent(tmp_path, version="0.2.0")._package_spec() == "rho-agent[evals]==0.2.0"
    git = _agent(tmp_path, install_source="git", version="abc123", repo_url="https://x/y.git")
    assert git._package_spec() == "rho-agent[evals] @ git+https://x/y.git@abc123"


async def test_install_builds_a_private_virtualenv(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path, install_source="git", version="main").install(env)

    install = env.calls[-1]["command"]
    assert "uv python install 3.12" in install
    assert 'uv venv --python 3.12 "$HOME/.rho-agent/venv"' in install
    assert "rho-agent[evals] @ git+" in install and "@main" in install
    assert "import rho_agent.eval.harbor.runner" in install
    assert env.calls[-1].get("user") is None  # runs as the agent user, not root


async def test_run_passes_instruction_settings_and_credentials(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    agent = _agent(
        tmp_path,
        bash_only=True,
        cost_ceiling_usd=4,
        max_wall_seconds=600,
        reasoning_effort="medium",
        enable_confirm_done=True,
    )

    await agent.run("fix the 'thing'", env, AgentContext())

    call = env.calls[-1]
    command, run_env = call["command"], call["env"]
    assert "-m rho_agent.eval.harbor.runner" in command
    assert "'fix the '\"'\"'thing'\"'\"''" in command  # shell-quoted instruction
    assert command.rstrip().endswith("tee /logs/agent/stdout.txt")
    assert " --bash-only " in command
    assert run_env["RHO_AGENT_MODEL"] == "openai/gpt-6.1-sol"
    assert run_env["OPENAI_API_KEY"] == "sk-test-not-real"
    assert run_env["RHO_AGENT_LOGS_DIR"] == "/logs/agent"
    assert run_env["RHO_AGENT_COST_CEILING_USD"] == "4.0"
    assert run_env["RHO_AGENT_MAX_WALL_SECONDS"] == "600.0"
    assert run_env["RHO_AGENT_REASONING_EFFORT"] == "medium"
    assert run_env["RHO_AGENT_CONFIRM_DONE"] == "1"
    # The key is not written into the command line, where it would be logged.
    assert "sk-test-not-real" not in command


async def test_run_omits_optional_settings_by_default(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path).run("task", env, AgentContext())

    run_env = env.calls[-1]["env"]
    assert "--bash-only" not in env.calls[-1]["command"]
    for name in (
        "RHO_AGENT_COST_CEILING_USD",
        "RHO_AGENT_MAX_WALL_SECONDS",
        "RHO_AGENT_TEMPERATURE",
        "RHO_AGENT_REASONING_EFFORT",
        "RHO_AGENT_ENABLE_REVIEWER",
    ):
        assert name not in run_env


def test_non_openai_models_get_their_own_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    env = _agent(tmp_path, model_name="anthropic/claude-opus-5-5")._runner_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-not-real"
    assert env["RHO_AGENT_MODEL"] == "anthropic/claude-opus-5-5"


def test_missing_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model is required"):
        RhoAgent(tmp_path)._runner_env()


def test_usage_is_read_from_tokens_json(tmp_path: Path) -> None:
    (tmp_path / "tokens.json").write_text(
        json.dumps({"input": 1200, "output": 300, "cached": 900, "cost_usd": 0.42})
    )
    context = AgentContext()
    _agent(tmp_path).populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (
        1200,
        300,
        900,
    )
    assert context.cost_usd == 0.42


def test_usage_falls_back_to_the_trace_when_tokens_json_is_missing(tmp_path: Path) -> None:
    events = [
        {"type": "run_start", "prompt": "go"},
        {
            "type": "llm_end",
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 80,
            "cost_usd": 0.1,
        },
        {
            "type": "llm_end",
            "input_tokens": 200,
            "output_tokens": 20,
            "cache_read_tokens": 150,
            "cost_usd": 0.2,
        },
    ]
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    context = AgentContext()
    _agent(tmp_path).populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (
        300,
        30,
        230,
    )
    assert abs(context.cost_usd - 0.3) < 1e-9
