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
    mark_invalid_parent,
    select_parent,
)
from rho_agent.evolve.harness import DomainHarness, load_harness
from rho_agent.evolve.models import EvolveConfig, Generation
from rho_agent.evolve.workspace import (
    build_agent_from_workspace,
    commit_pre_mutation,
    create_workspace,
    extract_diff,
    get_lineage,
    load_prompt_from_workspace,
    load_tools_from_workspace,
    materialize_workspace,
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

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        return [
            {"id": "v1", "question": "What is 7+7?", "expected": "14"},
            {"id": "v2", "question": "What is 9+1?", "expected": "10"},
        ][:n]


# --- Workspace tests ---


def test_create_workspace(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-0001-abc123")
    assert workspace.exists()
    assert (workspace / "tools").is_dir()
    assert (workspace / "lib").is_dir()
    assert (workspace / ".git").is_dir()
    assert workspace.name == "gen-0001-abc123"


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


# --- Diff tracking tests ---


def test_extract_diff(tmp_path: Path) -> None:
    """Extract a diff after modifying a workspace."""
    workspace = create_workspace(str(tmp_path), "gen-diff")
    commit_pre_mutation(workspace)

    # Simulate meta-agent writing a prompt
    (workspace / "prompt.md").write_text("You are a helpful assistant.")

    diff_path = extract_diff(workspace, str(tmp_path), "gen-diff")
    assert diff_path.exists()
    content = diff_path.read_text()
    assert "prompt.md" in content
    assert "helpful assistant" in content


def test_extract_diff_empty_mutation(tmp_path: Path) -> None:
    """No changes → empty diff file."""
    workspace = create_workspace(str(tmp_path), "gen-noop")
    commit_pre_mutation(workspace)

    diff_path = extract_diff(workspace, str(tmp_path), "gen-noop")
    assert diff_path.exists()
    assert diff_path.read_text().strip() == ""


def test_materialize_workspace(tmp_path: Path) -> None:
    """Materialize a workspace from a chain of diffs."""
    run_dir = str(tmp_path)

    # Gen 0: create prompt
    ws0 = create_workspace(run_dir, "g0")
    (ws0 / "prompt.md").write_text("version 1")
    extract_diff(ws0, run_dir, "g0")

    # Gen 1: modify prompt
    ws1 = create_workspace(run_dir, "g1-tmp")
    (ws1 / "prompt.md").write_text("version 1")
    commit_pre_mutation(ws1)
    (ws1 / "prompt.md").write_text("version 2")
    extract_diff(ws1, run_dir, "g1")

    archive = [
        Generation(gen_id="g0", generation=0, parent_id=None, workspace_path=str(ws0),
                   diff_path=str(tmp_path / "diffs" / "g0.diff"), created_at=""),
        Generation(gen_id="g1", generation=1, parent_id="g0", workspace_path=str(ws1),
                   diff_path=str(tmp_path / "diffs" / "g1.diff"), created_at=""),
    ]

    # Materialize gen 1 from scratch by replaying g0 + g1 diffs
    materialized = materialize_workspace(run_dir, "g1-rebuilt", archive, parent_id="g1")
    assert (materialized / "prompt.md").read_text() == "version 2"


def test_get_lineage() -> None:
    """Walk parent chain from root to target."""
    archive = [
        Generation(gen_id="g0", generation=0, parent_id=None, workspace_path="", created_at=""),
        Generation(gen_id="g1", generation=1, parent_id="g0", workspace_path="", created_at=""),
        Generation(gen_id="g2", generation=2, parent_id="g1", workspace_path="", created_at=""),
        Generation(gen_id="g3", generation=3, parent_id="g1", workspace_path="", created_at=""),  # branch
    ]
    chain = get_lineage("g2", archive)
    assert [g.gen_id for g in chain] == ["g0", "g1", "g2"]

    chain_branch = get_lineage("g3", archive)
    assert [g.gen_id for g in chain_branch] == ["g0", "g1", "g3"]

    chain_root = get_lineage("g0", archive)
    assert [g.gen_id for g in chain_root] == ["g0"]


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


def test_select_parent_score_child_prop(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    # g1 has high score but 3 children; g2 has lower score but 0 children
    append_generation(archive_path, _make_gen("g1", 0, score=0.9))
    append_generation(archive_path, _make_gen("g2", 1, score=0.5, parent_id="g1"))
    append_generation(archive_path, _make_gen("g3", 2, score=0.4, parent_id="g1"))
    append_generation(archive_path, _make_gen("g4", 3, score=0.3, parent_id="g1"))

    # Run many selections — g2 should be picked sometimes due to 0 children
    selected_ids = set()
    for _ in range(50):
        p = select_parent(archive_path, strategy="score_child_prop")
        assert p is not None
        selected_ids.add(p.gen_id)
    # Should select from multiple nodes, not just the best
    assert len(selected_ids) > 1


def test_select_parent_no_scored(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0))
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
    assert sample[0]["id"] == "v1"


def test_harness_staged_sample_not_implemented() -> None:
    """Base DomainHarness.staged_sample raises NotImplementedError."""

    class BareHarness(DomainHarness):
        def scenarios(self) -> list[dict[str, Any]]:
            return []

        async def run_agent(self, agent: Agent, scenario: dict[str, Any]) -> dict[str, Any]:
            return {}

        def score(self, results: list[dict[str, Any]]) -> float:
            return 0.0

    with pytest.raises(NotImplementedError):
        BareHarness().staged_sample(2)


# --- Sanitize results tests ---


def test_sanitize_results() -> None:
    from rho_agent.evolve.loop import _sanitize_results

    results = [
        {"scenario_id": "s1", "success": True, "expected": "4"},
        {"scenario_id": "s2", "success": False, "expected": "6", "error": "wrong"},
    ]
    sanitized = _sanitize_results(results)
    assert len(sanitized) == 2
    for r in sanitized:
        assert "expected" not in r
    assert sanitized[0]["scenario_id"] == "s1"
    assert sanitized[1]["error"] == "wrong"


# --- Config tests ---


def test_evolve_config_new_fields() -> None:
    config = EvolveConfig(
        harness="test.Harness",
        parent_strategy="best",
        meta_timeout=1800,
    )
    assert config.parent_strategy == "best"
    assert config.meta_timeout == 1800
    assert config.daytona_backend is None


def test_evolve_config_defaults() -> None:
    config = EvolveConfig(harness="test.Harness")
    assert config.parent_strategy == "score_child_prop"
    assert config.meta_timeout == 3600
    assert config.daytona_backend is None


def test_evolve_config_serializable() -> None:
    config = EvolveConfig(
        harness="test.Harness",
        model="gpt-5.4",
        task_model="gpt-5.4-mini",
        harness_kwargs={"train_n": "50"},
    )
    d = config.to_serializable_dict()
    roundtripped = json.loads(json.dumps(d))
    assert roundtripped["model"] == "gpt-5.4"
    assert roundtripped["task_model"] == "gpt-5.4-mini"
    assert roundtripped["daytona_backend"] is None
    assert roundtripped["harness_kwargs"] == {"train_n": "50"}


# --- Model tests ---


def test_generation_roundtrip() -> None:
    gen = _make_gen("g1", 0, score=0.75)
    d = gen.to_dict()
    restored = Generation.from_dict(d)
    assert restored.gen_id == "g1"
    assert restored.score == 0.75
    assert restored.status == "scored"


def test_generation_diff_path() -> None:
    gen = Generation(
        gen_id="g1", generation=0, parent_id=None,
        workspace_path="/tmp/ws/g1",
        diff_path="/tmp/diffs/g1.diff",
        created_at="",
    )
    d = gen.to_dict()
    restored = Generation.from_dict(d)
    assert restored.diff_path == "/tmp/diffs/g1.diff"


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


# --- Valid parent tracking tests ---


def test_mark_invalid_parent(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0, score=0.9))
    append_generation(archive_path, _make_gen("g2", 1, score=0.5, parent_id="g1"))

    mark_invalid_parent(archive_path, "g1")

    archive = load_archive(archive_path)
    assert archive[0].valid_parent is False
    assert archive[1].valid_parent is True


def test_select_parent_skips_invalid(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0, score=0.9))
    append_generation(archive_path, _make_gen("g2", 1, score=0.3, parent_id="g1"))

    # g1 is the best, but mark it invalid
    mark_invalid_parent(archive_path, "g1")

    parent = select_parent(archive_path, strategy="best")
    assert parent is not None
    assert parent.gen_id == "g2"  # falls back to g2


def test_select_parent_all_invalid(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.jsonl"
    append_generation(archive_path, _make_gen("g1", 0, score=0.9))
    mark_invalid_parent(archive_path, "g1")

    assert select_parent(archive_path, strategy="best") is None


# --- Workspace validation tests ---


def test_validate_workspace_valid(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _validate_workspace

    workspace = create_workspace(str(tmp_path), "gen-valid")
    (workspace / "tools" / "good.py").write_text("x = 1 + 2\n")
    assert _validate_workspace(workspace) is None


def test_validate_workspace_syntax_error(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _validate_workspace

    workspace = create_workspace(str(tmp_path), "gen-broken")
    (workspace / "tools" / "bad.py").write_text("def f(\n")
    error = _validate_workspace(workspace)
    assert error is not None
    assert "Syntax error" in error
    assert "bad.py" in error


def test_validate_workspace_lib_syntax_error(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _validate_workspace

    workspace = create_workspace(str(tmp_path), "gen-broken-lib")
    (workspace / "lib" / "helper.py").write_text("class Foo(:\n")
    error = _validate_workspace(workspace)
    assert error is not None
    assert "helper.py" in error


def test_validate_workspace_skips_underscored(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _validate_workspace

    workspace = create_workspace(str(tmp_path), "gen-skip")
    (workspace / "tools" / "__init__.py").write_text("def f(\n")  # broken but skipped
    assert _validate_workspace(workspace) is None


# --- Metacognitive self-modification tests ---


def test_create_workspace_has_memory_dir(tmp_path: Path) -> None:
    workspace = create_workspace(str(tmp_path), "gen-mem")
    assert (workspace / "memory").is_dir()


def test_render_meta_prompt_uses_workspace_template(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _render_meta_prompt

    workspace = create_workspace(str(tmp_path), "gen-meta")
    # Write a custom meta_prompt.md that includes a distinctive marker
    (workspace / "meta_prompt.md").write_text(
        "CUSTOM_MARKER gen={{ generation }} score={{ parent_score }}"
    )

    harness = TrivialHarness()
    result = _render_meta_prompt(
        generation=5,
        parent_score=0.75,
        best_score=0.9,
        parent_feedback="",
        lineage_summary="",
        workspace=workspace,
        harness=harness,
        scenario_sample=[],
    )
    assert "CUSTOM_MARKER" in result
    assert "gen=5" in result
    assert "score=0.75" in result


def test_render_meta_prompt_fallback_on_broken_template(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _render_meta_prompt

    workspace = create_workspace(str(tmp_path), "gen-broken-meta")
    # Write a broken Jinja2 template
    (workspace / "meta_prompt.md").write_text("{% if %}")

    harness = TrivialHarness()
    result = _render_meta_prompt(
        generation=1,
        parent_score=0.5,
        best_score=0.5,
        parent_feedback="",
        lineage_summary="",
        workspace=workspace,
        harness=harness,
        scenario_sample=[],
    )
    # Should fall back to built-in template (contains "meta-agent")
    assert "meta-agent" in result.lower()
    # Should NOT contain the broken template
    assert "{% if %}" not in result


def test_render_meta_prompt_includes_preamble(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _METACOGNITIVE_PREAMBLE, _render_meta_prompt

    workspace = create_workspace(str(tmp_path), "gen-preamble")
    harness = TrivialHarness()

    # Without workspace meta_prompt.md (uses built-in)
    result = _render_meta_prompt(
        generation=1,
        parent_score=0.5,
        best_score=0.5,
        parent_feedback="",
        lineage_summary="",
        workspace=workspace,
        harness=harness,
        scenario_sample=[],
    )
    assert result.startswith(_METACOGNITIVE_PREAMBLE)

    # With workspace meta_prompt.md
    (workspace / "meta_prompt.md").write_text("Custom template {{ generation }}")
    result2 = _render_meta_prompt(
        generation=1,
        parent_score=0.5,
        best_score=0.5,
        parent_feedback="",
        lineage_summary="",
        workspace=workspace,
        harness=harness,
        scenario_sample=[],
    )
    assert result2.startswith(_METACOGNITIVE_PREAMBLE)


def test_render_meta_prompt_task_agent_first_contract(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _render_meta_prompt

    workspace = create_workspace(str(tmp_path), "gen-contract")
    harness = TrivialHarness()

    result = _render_meta_prompt(
        generation=2,
        parent_score=0.4,
        best_score=0.6,
        parent_feedback="failed task x",
        lineage_summary="some history",
        workspace=workspace,
        harness=harness,
        scenario_sample=[],
    )

    assert "../" not in result
    assert "authoritative lineage context available in this workspace" in result
    assert "Treat task-agent behavior as the primary optimization target" in result


def test_build_performance_history() -> None:
    from rho_agent.evolve.loop import _build_performance_history

    archive = [
        _make_gen("g0", 0, score=0.3),
        _make_gen("g1", 1, score=0.5, parent_id="g0"),
        _make_gen("g2", 2, score=0.7, parent_id="g1"),
        _make_gen("g3", 3, score=None, parent_id="g2"),  # unscored
    ]
    history = _build_performance_history(archive)

    assert len(history["generations"]) == 3  # only scored
    assert history["statistics"]["best_score"] == 0.7
    assert history["statistics"]["worst_score"] == 0.3
    assert history["statistics"]["total_scored"] == 3


def test_build_performance_history_empty() -> None:
    from rho_agent.evolve.loop import _build_performance_history

    history = _build_performance_history([])
    assert history["generations"] == []
    assert history["statistics"] == {}


class _FakeProcessResponse:
    def __init__(self, exit_code: int, result: str = "") -> None:
        self.exit_code = exit_code
        self.result = result


class _FakeProcess:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def exec(self, command: str, timeout: int = 30) -> _FakeProcessResponse:
        if "find . -type f | sort" not in command:
            return _FakeProcessResponse(1, "unexpected command")
        lines = sorted(path.removeprefix("/home/daytona/workspace/") for path in self._files)
        return _FakeProcessResponse(0, "\n".join(lines))


class _FakeFS:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def download_file(self, remote_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(self._files[remote_path])


class _FakeSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.process = _FakeProcess(files)
        self.fs = _FakeFS(files)


@pytest.mark.asyncio
async def test_download_workspace_recurses_and_removes_deleted_files(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _download_workspace

    workspace = create_workspace(str(tmp_path), "gen-sync")
    stale_file = workspace / "tools" / "stale.py"
    stale_file.write_text("old")
    keep_file = workspace / "prompt.md"
    keep_file.write_text("outdated")

    remote_files = {
        "/home/daytona/workspace/prompt.md": b"fresh prompt",
        "/home/daytona/workspace/tools/docker_bash.py": b"class DockerBashHandler: pass\n",
        "/home/daytona/workspace/lib/helper.py": b"VALUE = 1\n",
        "/home/daytona/workspace/memory/notes.json": b'{"note": "kept"}',
    }
    sandbox = _FakeSandbox(remote_files)

    await _download_workspace(sandbox, workspace)

    assert keep_file.read_text() == "fresh prompt"
    assert (workspace / "tools" / "docker_bash.py").exists()
    assert (workspace / "lib" / "helper.py").exists()
    assert (workspace / "memory" / "notes.json").exists()
    assert not stale_file.exists()


@pytest.mark.asyncio
async def test_download_workspace_ignores_internal_and_unsafe_paths(tmp_path: Path) -> None:
    from rho_agent.evolve.loop import _download_workspace

    workspace = create_workspace(str(tmp_path), "gen-sync-skip")
    git_head_before = (workspace / ".git" / "HEAD").read_text()
    pycache_dir = workspace / "tools" / "__pycache__"
    pycache_dir.mkdir(parents=True, exist_ok=True)
    stale_pyc = pycache_dir / "stale.pyc"
    stale_pyc.write_bytes(b"old")

    remote_files = {
        "/home/daytona/workspace/prompt.md": b"fresh prompt",
        "/home/daytona/workspace/.git/HEAD": b"ref: refs/heads/hijacked\n",
        "/home/daytona/workspace/tools/__pycache__/cache.pyc": b"cache",
        "/home/daytona/workspace/../escape.txt": b"nope",
    }
    sandbox = _FakeSandbox(remote_files)

    await _download_workspace(sandbox, workspace)

    assert (workspace / "prompt.md").read_text() == "fresh prompt"
    assert (workspace / ".git" / "HEAD").read_text() == git_head_before
    assert not (workspace / "tools" / "__pycache__" / "cache.pyc").exists()
    assert stale_pyc.exists()
    assert not (workspace / "escape.txt").exists()


def test_memory_persists_through_diffs(tmp_path: Path) -> None:
    """Files in memory/ survive diff extraction and workspace materialization."""
    run_dir = str(tmp_path)

    # Gen 0: create workspace with a memory file
    ws0 = create_workspace(run_dir, "g0")
    (ws0 / "memory" / "notes.json").write_text('{"insight": "test"}')
    extract_diff(ws0, run_dir, "g0")

    archive = [
        Generation(
            gen_id="g0", generation=0, parent_id=None,
            workspace_path=str(ws0),
            diff_path=str(tmp_path / "diffs" / "g0.diff"),
            created_at="",
        ),
    ]

    # Materialize a child — memory should be inherited
    child = materialize_workspace(run_dir, "g1", archive, parent_id="g0")
    assert (child / "memory" / "notes.json").exists()
    data = json.loads((child / "memory" / "notes.json").read_text())
    assert data["insight"] == "test"


# --- Cross-run transfer tests ---


def test_evolve_config_transfer_from() -> None:
    config = EvolveConfig(harness="test.Harness", transfer_from="/tmp/old-run")
    assert config.transfer_from == "/tmp/old-run"
    d = config.to_serializable_dict()
    assert d["transfer_from"] == "/tmp/old-run"


def test_evolve_config_transfer_from_default() -> None:
    config = EvolveConfig(harness="test.Harness")
    assert config.transfer_from is None
