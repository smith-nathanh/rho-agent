"""BirdBench (text-to-SQL) domain harness.

Evaluates an agent's ability to generate correct SQL from natural language
questions. Uses the BIRD-Bench mini-dev dataset (500 tasks, 11 SQLite
databases). Scoring is execution accuracy: predicted SQL results must match
gold SQL results.

The meta-agent can improve the system prompt and add tools (e.g., schema
analyzers, query planners). The core tools (execute_sql, submit_sql) are
always registered by the harness and cannot be overridden.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ...core.agent import Agent
from ...core.session import Session
from ...eval.birdbench.evaluator import BirdEvaluator
from ...eval.birdbench.task import BirdTask, load_bird_tasks
from ...eval.birdbench.tools import BirdSqliteHandler, SubmitSqlHandler
from ..harness import DomainHarness

_DEFAULT_DATA_DIR = Path.home() / "proj" / "bird-bench-mini-dev" / "mini_dev_data"
_DEFAULT_DATA_FILE = _DEFAULT_DATA_DIR / "mini_dev_sqlite.json"
_DEFAULT_DB_DIR = _DEFAULT_DATA_DIR / "dev_databases"


def _stable_split(
    tasks: list[BirdTask],
    train_n: int,
    val_n: int,
    seed: int = 42,
) -> dict[str, list[BirdTask]]:
    """Deterministic split into train/val/test."""
    shuffled = list(tasks)
    random.Random(seed).shuffle(shuffled)
    return {
        "train": shuffled[:train_n],
        "val": shuffled[train_n : train_n + val_n],
        "test": shuffled[train_n + val_n :],
    }


class BirdBenchHarness(DomainHarness):
    """Text-to-SQL evaluation using BIRD-Bench mini-dev.

    The task agent receives a natural language question, optional evidence hint,
    and access to a SQLite database via execute_sql. It must explore the schema,
    write a SQL query, and submit it via submit_sql. Scoring is execution
    accuracy — the predicted query's results must match the gold query's results.

    Harness kwargs:
        data_file: Path to mini_dev_sqlite.json
        db_dir: Path to dev_databases/ directory
        train_n: Number of training scenarios (default: 50)
        val_n: Number of validation scenarios (default: 20)
        max_turns: Max agent turns per scenario (default: 15)
        turn_timeout: Per-turn timeout in seconds (default: 120)
        include_evidence: Include evidence hints (default: True)
        difficulty: Filter by difficulty: simple, moderate, challenging (default: all)
    """

    def __init__(
        self,
        data_file: str | None = None,
        db_dir: str | None = None,
        train_n: int | str = 50,
        val_n: int | str = 20,
        max_turns: int | str = 15,
        turn_timeout: int | str = 120,
        include_evidence: str | bool = True,
        difficulty: str | None = None,
    ) -> None:
        train_n = int(train_n)
        val_n = int(val_n)
        self._max_turns = int(max_turns)
        self._turn_timeout = int(turn_timeout)

        # Coerce string "True"/"False" from CLI
        if isinstance(include_evidence, str):
            include_evidence = include_evidence.lower() not in ("false", "0", "no")

        data_path = Path(data_file) if data_file else _DEFAULT_DATA_FILE
        db_path = Path(db_dir) if db_dir else _DEFAULT_DB_DIR

        if not data_path.exists():
            raise FileNotFoundError(
                f"BirdBench data not found: {data_path}\n"
                "Download from HuggingFace: BIRD-Bench/mini_dev"
            )

        all_tasks = load_bird_tasks(data_path, db_path, include_evidence, difficulty)
        self._splits = _stable_split(all_tasks, train_n, val_n)
        self._evaluator = BirdEvaluator(timeout=30)

    def scenarios(self) -> list[dict[str, Any]]:
        """Return training scenarios."""
        return [self._task_to_scenario(t) for t in self._splits["train"]]

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        """Return validation subset for quick filtering."""
        return [self._task_to_scenario(t) for t in self._splits["val"][:n]]

    def _task_to_scenario(self, task: BirdTask) -> dict[str, Any]:
        return {
            "id": str(task.question_id),
            "db_id": task.db_id,
            "difficulty": task.difficulty,
            "prompt": task.get_prompt(),
            "gold_sql": task.gold_sql,
            "db_path": task.db_path,
            "question": task.question,
        }

    async def run_agent(self, agent: Agent, scenario: dict[str, Any]) -> dict[str, Any]:
        """Run the agent on a single BirdBench task.

        Registers execute_sql and submit_sql alongside any workspace tools,
        copies the database to a temp file for safe exploration, and evaluates
        the submitted SQL against the gold SQL on the original database.
        """
        tmp_db_path = None
        handler = None

        try:
            # Copy DB to temp file so the agent can't corrupt the original
            tmp_fd, tmp_db_path = tempfile.mkstemp(
                suffix=".sqlite", prefix=f"bird_{scenario['db_id']}_"
            )
            os.close(tmp_fd)
            shutil.copy2(scenario["db_path"], tmp_db_path)

            # Register BirdBench tools (these coexist with workspace tools)
            handler = BirdSqliteHandler(tmp_db_path)
            agent.registry.register(handler)

            submitted_sql: str | None = None

            def capture_sql(sql: str) -> None:
                nonlocal submitted_sql
                submitted_sql = sql

            submit_handler = SubmitSqlHandler(on_submit=capture_sql)
            agent.registry.register(submit_handler)

            # Run session
            async with Session(agent) as session:
                for turn in range(self._max_turns):
                    if submit_handler.is_submitted:
                        break

                    turn_input = (
                        scenario["prompt"] if turn == 0
                        else "Continue working on the task."
                    )

                    try:
                        async with asyncio.timeout(self._turn_timeout):
                            result = await session.run(turn_input)
                    except TimeoutError:
                        break
                    except Exception:
                        break

                    if submit_handler.is_submitted:
                        break

            # Evaluate against original DB (read-only)
            bird_result = self._evaluator.evaluate(
                predicted_sql=submitted_sql,
                gold_sql=scenario["gold_sql"],
                db_path=scenario["db_path"],
                difficulty=scenario["difficulty"],
                db_id=scenario["db_id"],
            )

            return {
                "scenario_id": scenario["id"],
                "success": bird_result.is_correct,
                "db_id": scenario["db_id"],
                "difficulty": scenario["difficulty"],
                "predicted_sql": bird_result.predicted_sql,
                "gold_sql": bird_result.gold_sql,
                "error": bird_result.error,
            }

        except Exception as e:
            return {
                "scenario_id": scenario["id"],
                "success": False,
                "db_id": scenario.get("db_id", "unknown"),
                "difficulty": scenario.get("difficulty", "unknown"),
                "error": str(e),
            }

        finally:
            if handler:
                handler.close()
            # Unregister BirdBench tools so they don't leak to next scenario
            for name in ("execute_sql", "submit_sql"):
                if agent.registry.get(name):
                    agent.registry.unregister(name)
            if tmp_db_path and Path(tmp_db_path).exists():
                Path(tmp_db_path).unlink()

    def score(self, results: list[dict[str, Any]]) -> float:
        """Execution accuracy: fraction of correct SQL results."""
        if not results:
            return 0.0
        return sum(1 for r in results if r.get("success")) / len(results)

    def feedback(self, results: list[dict[str, Any]]) -> str:
        """Breakdown by difficulty, database, and common failure modes."""
        if not results:
            return "No results."

        total = len(results)
        correct = sum(1 for r in results if r.get("success"))
        accuracy = correct / total

        lines = [f"Overall: {correct}/{total} ({accuracy:.1%})"]

        # By difficulty
        for diff in ("simple", "moderate", "challenging"):
            subset = [r for r in results if r.get("difficulty") == diff]
            if subset:
                diff_correct = sum(1 for r in subset if r.get("success"))
                lines.append(
                    f"  {diff}: {diff_correct}/{len(subset)} "
                    f"({diff_correct / len(subset):.1%})"
                )

        # By database
        db_stats: dict[str, dict[str, int]] = {}
        for r in results:
            db = r.get("db_id", "unknown")
            if db not in db_stats:
                db_stats[db] = {"total": 0, "correct": 0}
            db_stats[db]["total"] += 1
            if r.get("success"):
                db_stats[db]["correct"] += 1

        worst_dbs = sorted(
            db_stats.items(),
            key=lambda x: x[1]["correct"] / max(x[1]["total"], 1),
        )
        if worst_dbs:
            lines.append("\nWeakest databases:")
            for db, stats in worst_dbs[:3]:
                acc = stats["correct"] / stats["total"]
                lines.append(f"  {db}: {stats['correct']}/{stats['total']} ({acc:.1%})")

        # Failure analysis
        failures = [r for r in results if not r.get("success")]
        no_submit = [f for f in failures if f.get("error") == "No SQL submitted"]
        sql_errors = [f for f in failures if f.get("error") and "error" in f["error"].lower()]
        wrong_results = [
            f for f in failures
            if f not in no_submit and f not in sql_errors
        ]

        if failures:
            lines.append(f"\nFailure breakdown ({len(failures)} total):")
            if no_submit:
                lines.append(f"  No SQL submitted: {len(no_submit)}")
            if sql_errors:
                lines.append(f"  SQL execution errors: {len(sql_errors)}")
            if wrong_results:
                lines.append(f"  Wrong results: {len(wrong_results)}")

        # Sample failures
        if failures:
            lines.append(f"\nSample failures ({min(5, len(failures))}):")
            for f in failures[:5]:
                lines.append(
                    f"  - {f['scenario_id']} ({f['db_id']}, {f['difficulty']}): "
                    f"{f.get('error') or 'wrong result'}"
                )

        return "\n".join(lines)
