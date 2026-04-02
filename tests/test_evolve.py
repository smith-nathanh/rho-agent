"""Tests for the evolve module."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rho_agent.core.agent import Agent
from rho_agent.evolve.archive import (
    append_generation,
    best_generation,
    load_archive,
    select_parent,
)
from rho_agent.evolve.harness import DomainHarness, load_harness
from rho_agent.evolve.models import EvolveConfig, Generation
from rho_agent.evolve.workspace import (
    build_agent_from_workspace,
    copy_workspace,
    create_workspace,
    load_prompt_from_workspace,
    load_tools_from_workspace,
)


# --- Fixtures ---


class TrivialHarness(DomainHarness):
    """A trivial harness for testing: scores based on prompt length."""

    def scenarios(self) -> list[dict[str, Any]]:
        return [
            {"id": "s1", "question": "What is 2+2?", "expected": "4"},
            {"id": "s2", "question": "What is 3+3?", "expected": "6"},
            {"id": "s3", "question": "What is 5+5?", "expected": "10"},
        ]

    async def run_agent(self, agent: Agent, scenario: dict[str, Any]) -> dict[str, Any]:
        # Don't actually run the agent — just check if prompt mentions math
        prompt = agent.config.system_prompt
        return {
            "scenario_id": scenario["id"],
            "success": "math" in prompt.lower() or "calculator" in prompt.lower(),
            "answer": scenario["expected"],
        }

    def score(self, results: list[dict[str, Any]]) -> float:
        if not results:
            return 0.0
        return sum(1.0 for r in results if r.get("success")) / len(results)


# --- Workspace tests ---


def test_create_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-0001-abc123")
    assert workspace.exists()
    assert (workspace / "tools").is_dir()
    assert (workspace / "lib").is_dir()
    assert workspace.name == "gen-0001-abc123"


def test_copy_workspace(tmp_path: Path) -> None:
    src = create_workspace(str(tmp_path), "gen-0001-src")
    (src / "prompt.md").write_text("You are helpful.")
    (src / "tools" / "my_tool.py").write_text("# tool code")

    dest = tmp_path / "workspaces" / "gen-0002-dest"
    copy_workspace(src, dest)

    assert (dest / "prompt.md").read_text() == "You are helpful."
    assert (dest / "tools" / "my_tool.py").read_text() == "# tool code"


def test_load_prompt_from_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-test")
    assert load_prompt_from_workspace(workspace) == ""

    (workspace / "prompt.md").write_text("You are a math tutor.")
    assert load_prompt_from_workspace(workspace) == "You are a math tutor."


def test_load_tools_from_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-tools")
    tool_code = textwrap.dedent("""\
        from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

        class AddTool(ToolHandler):
            @property
            def name(self) -> str:
                return "add"

            @property
            def description(self) -> str:
                return "Add two numbers"

            @property
            def parameters(self) -> dict:
                return {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                }

            async def handle(self, invocation: ToolInvocation) -> ToolOutput:
                a = invocation.arguments["a"]
                b = invocation.arguments["b"]
                return ToolOutput(content=str(a + b))
    """)
    (workspace / "tools" / "add_tool.py").write_text(tool_code)

    handlers = load_tools_from_workspace(workspace)
    assert len(handlers) == 1
    assert handlers[0].name == "add"


def test_load_tools_skips_underscored_files(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-skip")
    (workspace / "tools" / "__init__.py").write_text("")
    (workspace / "tools" / "_helpers.py").write_text("x = 1")

    handlers = load_tools_from_workspace(workspace)
    assert len(handlers) == 0


def test_load_tools_empty_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-empty")
    handlers = load_tools_from_workspace(workspace)
    assert handlers == []


# --- Archive tests ---


def _make_gen(gen_id: str, generation: int, score: float | None = None, **kwargs: Any) -> Generation:
    return Generation(
        gen_id=gen_id,
        generation=generation,
        parent_id=kwargs.get("parent_id"),
        workspace_path=f"/tmp/ws/{gen_id}",
        score=score,
        status="scored" if score is not None else "pending",
        created_at="2026-01-01T00:00:00Z",
        **{k: v for k, v in kwargs.items() if k != "parent_id"},
    )


def test_archive_append_and_load(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"

    gen1 = _make_gen("gen-0001-aaa", 0, score=0.5)
    gen2 = _make_gen("gen-0002-bbb", 1, score=0.8, parent_id="gen-0001-aaa")

    append_generation(archive_path, gen1)
    append_generation(archive_path, gen2)

    loaded = load_archive(archive_path)
    assert len(loaded) == 2
    assert loaded[0].gen_id == "gen-0001-aaa"
    assert loaded[1].gen_id == "gen-0002-bbb"
    assert loaded[1].score == 0.8


def test_best_generation(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"

    append_generation(archive_path, _make_gen("g1", 0, score=0.3))
    append_generation(archive_path, _make_gen("g2", 1, score=0.9))
    append_generation(archive_path, _make_gen("g3", 2, score=0.6))

    best = best_generation(archive_path)
    assert best is not None
    assert best.gen_id == "g2"
    assert best.score == 0.9


def test_best_generation_empty(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    assert best_generation(archive_path) is None


def test_select_parent_best(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0, score=0.3))
    append_generation(archive_path, _make_gen("g2", 1, score=0.9))

    parent = select_parent(archive_path, strategy="best")
    assert parent is not None
    assert parent.gen_id == "g2"


def test_select_parent_recent_best(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    for i in range(10):
        append_generation(archive_path, _make_gen(f"g{i}", i, score=float(i) / 10))

    parent = select_parent(archive_path, strategy="recent_best")
    assert parent is not None
    assert parent.gen_id == "g9"  # best of last 5


def test_select_parent_tournament(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0, score=0.5))
    append_generation(archive_path, _make_gen("g2", 1, score=0.7))

    parent = select_parent(archive_path, strategy="tournament")
    assert parent is not None
    assert parent.gen_id in ("g1", "g2")


def test_select_parent_no_scored(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0))  # no score
    assert select_parent(archive_path) is None


# --- Harness tests ---


def test_load_harness() -> None:
    harness = load_harness("tests.test_evolve.TrivialHarness")
    assert isinstance(harness, DomainHarness)
    scenarios = harness.scenarios()
    assert len(scenarios) == 3


def test_load_harness_invalid_path() -> None:
    with pytest.raises(ValueError):
        load_harness("NoModule")


def test_load_harness_not_subclass() -> None:
    with pytest.raises(TypeError):
        load_harness("json.JSONEncoder")


def test_harness_feedback() -> None:
    harness = TrivialHarness()
    results = [
        {"scenario_id": "s1", "success": True},
        {"scenario_id": "s2", "success": False, "error": "wrong answer"},
    ]
    feedback = harness.feedback(results)
    assert "Failed 1/2" in feedback
    assert "s2" in feedback


def test_harness_feedback_all_pass() -> None:
    harness = TrivialHarness()
    results = [{"scenario_id": "s1", "success": True}]
    assert harness.feedback(results) == "All scenarios passed."


def test_harness_staged_sample() -> None:
    harness = TrivialHarness()
    sample = harness.staged_sample(2)
    assert len(sample) == 2
    assert sample[0]["id"] == "s1"


# --- Model tests ---


def test_generation_roundtrip() -> None:
    gen = _make_gen("g1", 0, score=0.75)
    d = gen.to_dict()
    restored = Generation.from_dict(d)
    assert restored.gen_id == "g1"
    assert restored.score == 0.75
    assert restored.status == "scored"


def test_evolve_config_effective_task_model() -> None:
    config = EvolveConfig(harness="test.Harness", model="gpt-5")
    assert config.effective_task_model == "gpt-5"

    config2 = EvolveConfig(harness="test.Harness", model="gpt-5", task_model="gpt-5-mini")
    assert config2.effective_task_model == "gpt-5-mini"


# --- Build agent test ---


def test_build_agent_from_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-build")
    (workspace / "prompt.md").write_text("You are a math expert.")

    config = EvolveConfig(harness="test.Harness", model="gpt-5-mini")
    agent = build_agent_from_workspace(workspace, config)

    assert agent.config.system_prompt == "You are a math expert."
    assert agent.config.profile == "unrestricted"
    assert agent.config.model == "gpt-5-mini"
