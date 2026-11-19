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
    for name in (
        "RHO_AGENT_API_KEY",
        "RHO_AGENT_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENROUTER_API_BASE",
    ):
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
    assert _agent(tmp_path)._package_spec() == "rho-agent"
    assert _agent(tmp_path, version="0.2.0")._package_spec() == "rho-agent==0.2.0"
    git = _agent(tmp_path, install_source="git", version="abc123", repo_url="https://x/y.git")
    assert git._package_spec() == "rho-agent @ git+https://x/y.git@abc123"
    # GitHub repos install from the source archive, which needs no git in the sandbox.
    gh = _agent(tmp_path, install_source="git", version="abc123")
    assert gh._package_spec() == (
        "rho-agent @ https://github.com/smith-nathanh/rho-agent/archive/abc123.tar.gz"
    )
    head = _agent(tmp_path, install_source="git")
    assert head._package_spec().endswith("/archive/HEAD.tar.gz")


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/smith-nathanh/rho-agent",
        "https://github.com/smith-nathanh/rho-agent.git/",
        "http://www.github.com/smith-nathanh/rho-agent",
        "git@github.com:smith-nathanh/rho-agent.git",
        "ssh://git@github.com/smith-nathanh/rho-agent.git",
    ],
)
def test_github_urls_use_the_archive(tmp_path: Path, repo_url: str) -> None:
    agent = _agent(tmp_path, install_source="git", version="feature/x y", repo_url=repo_url)
    assert agent._package_spec() == (
        "rho-agent @ https://github.com/smith-nathanh/rho-agent/archive/feature/x%20y.tar.gz"
    )


async def test_install_builds_a_private_virtualenv(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path, install_source="git", version="main").install(env)

    install = env.calls[-1]["command"]
    assert "uv python install 3.12" in install
    assert 'uv venv --python 3.12 "$HOME/.rho-agent/venv"' in install
    assert "rho-agent @ https://github.com/smith-nathanh/rho-agent/archive/main.tar.gz" in install
    assert "import rho_agent.eval.harbor.runner" in install
    assert env.calls[-1].get("user") is None  # runs as the agent user, not root

    # Before any apt install, bullseye images get the archived security repo.
    repoint = env.calls[0]
    assert repoint.get("user") == "root"
    assert "bullseye" in repoint["command"]
    assert "archive.debian.org/debian-security" in repoint["command"]


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
    # ModelClient takes the bare model name and reads only RHO_AGENT_* variables.
    assert run_env["RHO_AGENT_MODEL"] == "gpt-6.1-sol"
    assert run_env["RHO_AGENT_API_KEY"] == "sk-test-not-real"
    assert run_env["RHO_AGENT_BASE_URL"] == "https://api.openai.com/v1"
    assert "OPENAI_API_KEY" not in run_env
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
        "RHO_AGENT_AUTO_COMPACT",
        "RHO_AGENT_COMPACT_THRESHOLD",
        "RHO_AGENT_COMPACT_AT_TOKENS",
    ):
        assert name not in run_env


async def test_run_passes_view_image_only_when_set(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path, bash_only=True).run("task", env, AgentContext())
    assert "RHO_AGENT_VIEW_IMAGE" not in env.calls[-1]["env"]

    await _agent(tmp_path, bash_only=True, view_image="true").run("task", env, AgentContext())
    assert env.calls[-1]["env"]["RHO_AGENT_VIEW_IMAGE"] == "1"
    assert " --bash-only " in env.calls[-1]["command"]

    await _agent(tmp_path, view_image=False).run("task", env, AgentContext())
    assert env.calls[-1]["env"]["RHO_AGENT_VIEW_IMAGE"] == "0"


async def test_run_passes_compaction_settings(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    agent = _agent(tmp_path, auto_compact=False, compact_threshold="0.25", compact_at_tokens=40000)

    await agent.run("task", env, AgentContext())

    run_env = env.calls[-1]["env"]
    assert run_env["RHO_AGENT_AUTO_COMPACT"] == "0"
    assert run_env["RHO_AGENT_COMPACT_THRESHOLD"] == "0.25"
    assert run_env["RHO_AGENT_COMPACT_AT_TOKENS"] == "40000"


@pytest.mark.parametrize("kwargs", [{"compact_threshold": 1.5}, {"compact_at_tokens": 0}])
def test_bad_compaction_settings_are_rejected(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _agent(tmp_path, **kwargs)


def test_openrouter_models_get_openrouters_key_and_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    env = _agent(tmp_path, model_name="openrouter/openai/gpt-6.1-sol")._runner_env()
    assert env["RHO_AGENT_API_KEY"] == "sk-or-test-not-real"
    assert env["RHO_AGENT_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["RHO_AGENT_MODEL"] == "openai/gpt-6.1-sol"  # OpenRouter's name for it


def test_a_server_you_name_needs_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    env = _agent(tmp_path, model_name="openai/my-model")._runner_env()
    assert env["RHO_AGENT_BASE_URL"] == "http://localhost:8000/v1"
    assert "RHO_AGENT_API_KEY" not in env


def test_openai_without_a_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(Exception, match="API key"):
        _agent(tmp_path)._runner_env()


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


def test_skills_and_mcp_capabilities_are_on() -> None:
    caps = RhoAgent.capabilities
    assert caps.skills and caps.mcp_servers and caps.mcp_http_headers


async def test_run_copies_skills_before_the_runner(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path, skills_dir="/opt/task skills").run("task", env, AgentContext())

    copy, run = env.calls[-2], env.calls[-1]
    assert copy["command"].endswith(
        'mkdir -p "$HOME/.agents/skills" && '
        "cp -R '/opt/task skills/.' \"$HOME/.agents/skills/\" || "
        "echo 'rho-agent: could not copy skills from ''/opt/task skills/.' >&2"
    )
    assert copy.get("user") is None  # as the agent user, whose HOME the runner reads
    assert "-m rho_agent.eval.harbor.runner" in run["command"]
    assert run["env"]["RHO_AGENT_TASK_SKILLS"] == "1"


async def test_run_without_skills_or_mcp_adds_nothing(tmp_path: Path) -> None:
    env = _FakeEnvironment()
    await _agent(tmp_path).run("task", env, AgentContext())

    assert len(env.calls) == 1
    assert "RHO_AGENT_TASK_SKILLS" not in env.calls[0]["env"]
    assert "RHO_AGENT_MCP_SERVERS" not in env.calls[0]["env"]
    assert "[mcp]" not in _agent(tmp_path, install_source="git")._package_spec()


async def test_run_passes_mcp_servers_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harbor.models.task.config import MCPServerConfig

    monkeypatch.setenv("PW_TOKEN", "secret-token")
    servers = [
        MCPServerConfig(name="playwright", transport="sse", url="http://playwright-mcp:3080/sse"),
        MCPServerConfig(
            name="api",
            transport="http",
            url="http://api:8080/mcp",
            headers={"Authorization": "Bearer ${PW_TOKEN}"},
        ),
        MCPServerConfig(name="local", transport="stdio", command="npx", args=["srv"]),
    ]
    env = _FakeEnvironment()
    agent = _agent(tmp_path, mcp_servers=servers, install_source="git", version="main")
    await agent.run("task", env, AgentContext())

    run_env = env.calls[-1]["env"]
    passed = json.loads(run_env["RHO_AGENT_MCP_SERVERS"])
    assert passed[0] == {
        "name": "playwright",
        "transport": "sse",
        "url": "http://playwright-mcp:3080/sse",
    }
    assert passed[2] == {"name": "local", "transport": "stdio", "command": "npx", "args": ["srv"]}
    # The header names a private variable that carries the resolved value.
    [(header, ref)] = passed[1]["headers"].items()
    assert passed[1]["transport"] == "streamable-http" and header == "Authorization"
    variable = ref.removeprefix("${").removesuffix("}")
    assert run_env[variable] == "Bearer secret-token"
    assert "secret-token" not in run_env["RHO_AGENT_MCP_SERVERS"]
    assert "secret-token" not in env.calls[-1]["command"]

    # The runner reads the JSON back into the same servers.
    from rho_agent.tools.mcp_client import parse_mcp_servers

    assert [s.name for s in parse_mcp_servers(run_env["RHO_AGENT_MCP_SERVERS"])] == [
        "playwright",
        "api",
        "local",
    ]
    # The mcp extra is installed only when there are servers.
    assert agent._package_spec().startswith("rho-agent[mcp] @ https://github.com/")
