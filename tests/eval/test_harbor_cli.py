from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from rho_agent.eval.harbor.cli import app

runner = CliRunner()


def test_list_configs_includes_bundled_templates() -> None:
    result = runner.invoke(app, ["list-configs"])

    assert result.exit_code == 0
    assert "terminal-bench-subset.yaml" in result.stdout
    assert "terminal-bench.yaml" in result.stdout


def test_write_config_supports_stem_name(tmp_path: Path) -> None:
    output = tmp_path / "sample.yaml"

    result = runner.invoke(
        app,
        ["write-config", "terminal-bench-subset", "--output", str(output)],
    )

    assert result.exit_code == 0
    assert output.exists()
    assert "import_path: rho_agent.eval.harbor.agent:RhoAgent" in output.read_text()
    assert "install_source: git" in output.read_text()
    assert "n_tasks: 20" in output.read_text()


def test_write_config_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "terminal-bench.yaml"
    output.write_text("existing")

    result = runner.invoke(
        app,
        ["write-config", "terminal-bench", "--output", str(output)],
    )

    assert result.exit_code != 0
    assert output.read_text() == "existing"


def test_bundled_configs_match_harbor_schema_and_agent_options(monkeypatch) -> None:
    """Configs must parse with the installed Harbor and use real agent options."""
    import pytest

    pytest.importorskip("harbor", reason="harbor extra not installed")
    import yaml
    from harbor.models.job.config import JobConfig

    from rho_agent.eval.harbor.agent import RhoAgent
    from rho_agent.eval.harbor.cli import _available_configs

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    names = {n for n in _available_configs() if n.endswith(".yaml")}
    assert names == {"terminal-bench.yaml", "terminal-bench-subset.yaml"}
    for name in names:
        config = JobConfig.model_validate(yaml.safe_load(_available_configs()[name].read_text()))
        (agent,) = config.agents
        assert agent.import_path == RhoAgent.import_path()
        options = RhoAgent.parse_options(dict(agent.kwargs))
        assert options.cost_ceiling_usd > 0, "every bundled config must cap cost"
        assert options.max_wall_seconds > 0
        assert config.n_attempts == 1
