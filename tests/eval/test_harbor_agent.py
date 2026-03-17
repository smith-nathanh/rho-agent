from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest


def _load_agent_module():
    harbor = types.ModuleType("harbor")
    harbor.__path__ = []

    agents = types.ModuleType("harbor.agents")
    agents.__path__ = []
    installed = types.ModuleType("harbor.agents.installed")
    installed.__path__ = []
    base = types.ModuleType("harbor.agents.installed.base")

    @dataclass
    class ExecInput:
        command: str
        cwd: str | None = None
        env: dict[str, str] | None = None
        timeout_sec: int | None = None

    class BaseInstalledAgent:
        def __init__(
            self,
            logs_dir: Path,
            *args,
            model_name: str | None = None,
            logger=None,
            version: str | None = None,
            **kwargs,
        ) -> None:
            self.logs_dir = logs_dir
            self.model_name = model_name
            self.logger = logger
            self._version = version

    base.BaseInstalledAgent = BaseInstalledAgent
    base.ExecInput = ExecInput

    models = types.ModuleType("harbor.models")
    models.__path__ = []
    agent_pkg = types.ModuleType("harbor.models.agent")
    agent_pkg.__path__ = []
    context_mod = types.ModuleType("harbor.models.agent.context")

    class AgentContext:
        pass

    context_mod.AgentContext = AgentContext

    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.agents": agents,
            "harbor.agents.installed": installed,
            "harbor.agents.installed.base": base,
            "harbor.models": models,
            "harbor.models.agent": agent_pkg,
            "harbor.models.agent.context": context_mod,
        }
    )

    sys.modules.pop("rho_agent.eval.harbor.agent", None)
    return importlib.import_module("rho_agent.eval.harbor.agent")


@pytest.fixture
def harbor_agent_module():
    return _load_agent_module()


def test_default_template_variables_use_pypi_install(harbor_agent_module) -> None:
    agent = harbor_agent_module.RhoAgent(logs_dir=Path("/tmp/logs"))

    assert agent._template_variables == {
        "install_source": "pypi",
        "repo_url": "https://github.com/smith-nathanh/rho-agent.git",
        "venv_path": "/opt/rho-agent-venv",
    }


def test_git_install_template_variables_include_version(harbor_agent_module) -> None:
    agent = harbor_agent_module.RhoAgent(
        logs_dir=Path("/tmp/logs"),
        install_source="git",
        repo_url="https://example.com/custom/rho-agent.git",
        version="feature/harbor-fix",
    )

    assert agent._template_variables == {
        "install_source": "git",
        "repo_url": "https://example.com/custom/rho-agent.git",
        "venv_path": "/opt/rho-agent-venv",
        "version": "feature/harbor-fix",
    }


def test_invalid_install_source_raises(harbor_agent_module) -> None:
    with pytest.raises(ValueError, match="install_source"):
        harbor_agent_module.RhoAgent(logs_dir=Path("/tmp/logs"), install_source="local")


def test_run_command_uses_stable_container_venv(harbor_agent_module, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "openai/gpt-5-mini")
    agent = harbor_agent_module.RhoAgent(logs_dir=Path("/tmp/logs"), bash_only=True)

    [command] = agent.create_run_agent_commands("solve task")

    assert command.env is not None
    assert command.env["OPENAI_API_KEY"] == "sk-test"
    assert command.env["RHO_AGENT_MODEL"] == "gpt-5-mini"
    assert "/opt/rho-agent-venv/bin/python -B -m rho_agent.eval.harbor.runner" in command.command
    assert "/rho-agent/.venv/bin/python" not in command.command
    assert "--bash-only" in command.command
