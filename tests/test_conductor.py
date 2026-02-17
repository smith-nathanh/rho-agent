"""Tests for the conductor module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rho_agent.conductor.models import (
    ConductorConfig,
    ConductorState,
    Task,
    TaskDAG,
    TaskStatus,
    TaskUsage,
    VerificationConfig,
)
from rho_agent.conductor.state import (
    load_state,
    save_state,
    state_path_for_run,
    latest_state_path,
)
from rho_agent.conductor.checks import run_checks
from rho_agent.conductor.planner import _extract_json, _build_dag
from rho_agent.conductor.worker import _is_over_budget, _is_task_complete


# ---------------------------------------------------------------------------
# models tests
# ---------------------------------------------------------------------------


def test_task_status_values():
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.IN_PROGRESS.value == "in_progress"
    assert TaskStatus.DONE.value == "done"
    assert TaskStatus.FAILED.value == "failed"


def _make_dag() -> TaskDAG:
    t1 = Task(
        id="T1",
        title="Setup",
        description="Setup project",
        acceptance_criteria=["project compiles"],
    )
    t2 = Task(
        id="T2",
        title="Feature A",
        description="Implement feature A",
        acceptance_criteria=["feature works"],
        depends_on=["T1"],
    )
    t3 = Task(
        id="T3",
        title="Feature B",
        description="Implement feature B",
        acceptance_criteria=["feature works"],
        depends_on=["T1"],
    )
    t4 = Task(
        id="T4",
        title="Integration",
        description="Integrate A and B",
        acceptance_criteria=["tests pass"],
        depends_on=["T2", "T3"],
    )
    return TaskDAG(
        project_name="test-project",
        tasks={"T1": t1, "T2": t2, "T3": t3, "T4": t4},
        verification=VerificationConfig(test_cmd="pytest"),
    )


def test_dag_ready_tasks_initial():
    dag = _make_dag()
    ready = dag.ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "T1"


def test_dag_ready_tasks_after_t1_done():
    dag = _make_dag()
    dag.tasks["T1"].status = TaskStatus.DONE
    ready = dag.ready_tasks()
    ids = sorted(t.id for t in ready)
    assert ids == ["T2", "T3"]


def test_dag_next_ready_task():
    dag = _make_dag()
    assert dag.next_ready_task().id == "T1"


def test_dag_all_done():
    dag = _make_dag()
    assert not dag.all_done()
    for t in dag.tasks.values():
        t.status = TaskStatus.DONE
    assert dag.all_done()


def test_dag_no_ready_when_blocked():
    dag = _make_dag()
    dag.tasks["T1"].status = TaskStatus.FAILED
    # T2 and T3 depend on T1 which failed, so they aren't ready
    # (ready_tasks only checks if dep status is DONE)
    assert dag.next_ready_task() is None


def test_dag_has_remaining_work():
    dag = _make_dag()
    assert dag.has_remaining_work()
    for t in dag.tasks.values():
        t.status = TaskStatus.DONE
    assert not dag.has_remaining_work()


# ---------------------------------------------------------------------------
# state serialization tests
# ---------------------------------------------------------------------------


def test_state_round_trip(tmp_path):
    config = ConductorConfig(
        prd_path="/tmp/prd.md",
        working_dir="/tmp/proj",
        service_tier="flex",
    )
    dag = _make_dag()
    dag.tasks["T1"].status = TaskStatus.DONE
    dag.tasks["T1"].commit_sha = "abc123"
    usage = {
        "T1": TaskUsage(
            task_id="T1",
            worker_input_tokens=1000,
            worker_output_tokens=500,
            worker_cost_usd=0.01,
            worker_sessions=1,
        )
    }
    state = ConductorState(
        run_id="test-123",
        config=config,
        dag=dag,
        usage=usage,
        status="running",
    )

    path = tmp_path / "state.json"
    save_state(path, state)
    assert path.exists()

    loaded = load_state(path)
    assert loaded.run_id == "test-123"
    assert loaded.dag.project_name == "test-project"
    assert loaded.dag.tasks["T1"].status == TaskStatus.DONE
    assert loaded.dag.tasks["T1"].commit_sha == "abc123"
    assert loaded.dag.tasks["T2"].status == TaskStatus.PENDING
    assert loaded.usage["T1"].worker_input_tokens == 1000
    assert loaded.config.service_tier == "flex"


def test_state_load_resets_in_progress(tmp_path):
    """On resume, stale IN_PROGRESS tasks should be reset to PENDING."""
    config = ConductorConfig(prd_path="/tmp/prd.md")
    dag = _make_dag()
    dag.tasks["T1"].status = TaskStatus.IN_PROGRESS
    state = ConductorState(run_id="x", config=config, dag=dag)

    path = tmp_path / "state.json"
    save_state(path, state)

    loaded = load_state(path)
    assert loaded.dag.tasks["T1"].status == TaskStatus.PENDING


def test_state_path_for_run():
    path = state_path_for_run("abc123")
    assert path.name == "abc123.json"
    assert "conductor" in str(path)


def test_latest_state_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    state_dir = tmp_path / ".config" / "rho-agent" / "conductor"
    state_dir.mkdir(parents=True)
    older = state_dir / "older.json"
    newer = state_dir / "newer.json"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    newer.touch()
    assert latest_state_path() == newer


# ---------------------------------------------------------------------------
# planner helpers
# ---------------------------------------------------------------------------


def test_extract_json_from_markdown():
    text = """Here is the plan:
```json
{"project_name": "foo", "tasks": [], "verification": {}}
```
Done."""
    result = _extract_json(text)
    assert result["project_name"] == "foo"


def test_extract_json_no_json():
    with pytest.raises(ValueError, match="No JSON"):
        _extract_json("no json here")


def test_build_dag_with_config_overrides():
    config = ConductorConfig(
        prd_path="/tmp/prd.md",
        test_cmd="pytest -x",
        lint_cmd="ruff check .",
    )
    raw = {
        "project_name": "test",
        "verification": {"test_cmd": "pytest", "lint_cmd": None, "typecheck_cmd": "mypy"},
        "tasks": [
            {
                "id": "T1",
                "title": "Do thing",
                "description": "Do the thing",
                "acceptance_criteria": ["done"],
                "depends_on": [],
            }
        ],
    }
    dag = _build_dag(raw, config)
    # Config overrides planner suggestions
    assert dag.verification.test_cmd == "pytest -x"
    assert dag.verification.lint_cmd == "ruff check ."
    # Planner suggestion preserved when no config override
    assert dag.verification.typecheck_cmd == "mypy"
    assert "T1" in dag.tasks


# ---------------------------------------------------------------------------
# worker helpers
# ---------------------------------------------------------------------------


def test_is_over_budget():
    assert _is_over_budget(90_000, 128_000, 0.7)
    assert not _is_over_budget(80_000, 128_000, 0.7)
    assert _is_over_budget(89_600, 128_000, 0.7)  # exactly at threshold


def test_is_task_complete():
    assert _is_task_complete("Task complete")
    assert _is_task_complete("... TASK COMPLETE ...")
    assert not _is_task_complete("still working")


# ---------------------------------------------------------------------------
# checks tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checks_no_commands():
    v = VerificationConfig()
    result = await run_checks(v, "/tmp")
    assert result.passed
    assert result.output == ""


@pytest.mark.asyncio
async def test_checks_passing_command(tmp_path):
    v = VerificationConfig(test_cmd="true")
    result = await run_checks(v, str(tmp_path))
    assert result.passed


@pytest.mark.asyncio
async def test_checks_failing_command(tmp_path):
    v = VerificationConfig(test_cmd="false")
    result = await run_checks(v, str(tmp_path))
    assert not result.passed
    assert "test" in result.output


@pytest.mark.asyncio
async def test_checks_reject_shell_operators(tmp_path):
    v = VerificationConfig(test_cmd="echo ok && false")
    result = await run_checks(v, str(tmp_path))
    assert not result.passed
    assert "Disallowed shell operators" in result.output


# ---------------------------------------------------------------------------
# state with no DAG
# ---------------------------------------------------------------------------


def test_state_without_dag(tmp_path):
    config = ConductorConfig(prd_path="/tmp/prd.md")
    state = ConductorState(run_id="no-dag", config=config)
    path = tmp_path / "state.json"
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.dag is None
    assert loaded.run_id == "no-dag"


# ---------------------------------------------------------------------------
# DAG validation (issue #4: unknown/cyclic deps)
# ---------------------------------------------------------------------------


def test_dag_missing_dep_blocks_ready():
    """A task with an unknown dependency must never become ready."""
    t1 = Task(
        id="T1",
        title="Root",
        description="Root task",
        acceptance_criteria=["done"],
        depends_on=["MISSING"],
    )
    dag = TaskDAG(
        project_name="test",
        tasks={"T1": t1},
        verification=VerificationConfig(),
    )
    assert dag.ready_tasks() == []
    assert dag.next_ready_task() is None


def test_build_dag_rejects_unknown_deps():
    config = ConductorConfig(prd_path="/tmp/prd.md")
    raw = {
        "project_name": "test",
        "verification": {},
        "tasks": [
            {
                "id": "T1",
                "title": "Do thing",
                "description": "Do the thing",
                "acceptance_criteria": ["done"],
                "depends_on": ["NONEXISTENT"],
            }
        ],
    }
    with pytest.raises(ValueError, match="unknown tasks"):
        _build_dag(raw, config)


def test_build_dag_rejects_cycle():
    config = ConductorConfig(prd_path="/tmp/prd.md")
    raw = {
        "project_name": "test",
        "verification": {},
        "tasks": [
            {
                "id": "T1",
                "title": "A",
                "description": "A",
                "acceptance_criteria": [],
                "depends_on": ["T2"],
            },
            {
                "id": "T2",
                "title": "B",
                "description": "B",
                "acceptance_criteria": [],
                "depends_on": ["T1"],
            },
        ],
    }
    with pytest.raises(ValueError, match="cycle"):
        _build_dag(raw, config)
