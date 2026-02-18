"""Evaluation runner for BIRD-Bench tasks.

For each task:
1. Copies the database to a temp file (agent works on the copy, original stays safe)
2. Creates execute_sql + submit_sql tools pointing at the copy
3. Runs the agent conversation
4. Evaluates the submitted SQL against gold SQL on the ORIGINAL database (read-only)
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from rho_agent.capabilities import (
    ApprovalMode,
    CapabilityProfile,
    DatabaseMode,
    FileWriteMode,
    ShellMode,
)
from rho_agent.prompts import load_prompt
from rho_agent.runtime import RuntimeOptions, close_runtime, create_runtime, run_prompt, start_runtime

from .config import BirdMetrics, EvalAbortedError, EvalConfig, TaskResult, TaskStatus
from .evaluator import BirdEvaluator
from .output import append_result, update_overall
from .task import BirdTask
from .tools import BirdSqliteHandler, SubmitSqlHandler


_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_BIRD_PROMPT = _PROMPTS_DIR / "eval_bird.md"


class BirdRunner:
    """Orchestrates running BIRD-Bench evaluation tasks."""

    def __init__(self, config: EvalConfig) -> None:
        self.config = config
        self._evaluator = BirdEvaluator()

    def _get_system_prompt(self) -> str:
        if self.config.system_prompt_file:
            try:
                prompt = load_prompt(self.config.system_prompt_file)
                return prompt.system_prompt
            except Exception:
                pass
        prompt = load_prompt(_BIRD_PROMPT)
        return prompt.system_prompt

    async def run_task(self, task: BirdTask) -> TaskResult:
        """Run a single BIRD-Bench task.

        Copies the database to a temp file for the agent, then evaluates
        the submitted SQL against the gold SQL on the original database.
        """
        tmp_db_path = None
        handler = None
        runtime = None

        try:
            # Copy database to temp file (agent safety)
            tmp_fd, tmp_db_path = tempfile.mkstemp(
                suffix=".sqlite",
                prefix=f"bird_{task.db_id}_",
            )
            os.close(tmp_fd)
            shutil.copy2(task.db_path, tmp_db_path)

            # Build runtime with minimal profile (only custom tools needed)
            system_prompt = self._get_system_prompt()
            profile = CapabilityProfile(
                name="birdbench",
                description="BIRD-Bench SQL evaluation profile",
                shell=ShellMode.RESTRICTED,
                file_write=FileWriteMode.OFF,
                database=DatabaseMode.READONLY,
                approval=ApprovalMode.NONE,
            )
            options = RuntimeOptions(
                model=self.config.model,
                base_url=self.config.base_url,
                service_tier=self.config.service_tier,
                profile=profile,
                auto_approve=True,
                enable_delegate=False,
            )
            runtime = create_runtime(system_prompt, options=options)
            runtime.registry.clear()

            # Register BIRD-specific tools
            handler = BirdSqliteHandler(db_path=tmp_db_path)
            runtime.registry.register(handler)

            submitted_sql: str | None = None

            def capture_sql(sql: str) -> None:
                nonlocal submitted_sql
                submitted_sql = sql

            submit_handler = SubmitSqlHandler(on_submit=capture_sql)
            runtime.registry.register(submit_handler)

            await start_runtime(runtime)

            # Run the task
            prompt = task.get_prompt()
            status = TaskStatus.COMPLETED
            turns = 0
            consecutive_timeouts = 0
            turn_timeout = 600 if self.config.service_tier == "flex" else 120

            for turn in range(self.config.max_turns):
                turns += 1

                if submit_handler.is_submitted:
                    break

                turn_input = prompt if turn == 0 else "Continue working on the task."

                try:
                    async with asyncio.timeout(turn_timeout):
                        result = await run_prompt(runtime, turn_input)

                    consecutive_timeouts = 0

                    if result.status == "error":
                        print(
                            f"[Task {task.index}] API Error in turn {turn}",
                            file=sys.stderr,
                        )

                    if submit_handler.is_submitted:
                        break

                except TimeoutError:
                    consecutive_timeouts += 1
                    print(
                        f"[Task {task.index}] Turn {turn} timed out ({consecutive_timeouts}/3)",
                        file=sys.stderr,
                    )
                    if consecutive_timeouts >= 3:
                        raise EvalAbortedError(
                            "Aborting: 3 consecutive turn timeouts.",
                            consecutive_timeouts,
                        )
                except Exception as e:
                    if "context" in str(e).lower():
                        status = TaskStatus.AGENT_CONTEXT_LIMIT
                    else:
                        status = TaskStatus.TASK_ERROR
                    break

            if turns >= self.config.max_turns and not submit_handler.is_submitted:
                status = TaskStatus.TASK_LIMIT_REACHED

            # Evaluate: run both SQL on the ORIGINAL database (read-only)
            bird_result = self._evaluator.evaluate(
                predicted_sql=submitted_sql,
                gold_sql=task.gold_sql,
                db_path=task.db_path,  # original, not copy
                difficulty=task.difficulty,
                db_id=task.db_id,
            )

            return TaskResult(
                index=task.index,
                status=status,
                history=runtime.session.history.copy(),
                time=TaskResult.create_time(),
                result=bird_result,
            )

        except EvalAbortedError:
            raise
        except Exception as e:
            return TaskResult(
                index=task.index,
                status=TaskStatus.TASK_ERROR,
                history=[],
                time=TaskResult.create_time(),
                error=str(e),
            )

        finally:
            if runtime:
                await close_runtime(runtime, status="completed")
            if handler:
                handler.close()
            if tmp_db_path and Path(tmp_db_path).exists():
                Path(tmp_db_path).unlink()

    async def run_tasks(
        self,
        tasks: list[BirdTask],
        output_dir: Path | str,
        progress_callback: Any = None,
        initial_metrics: BirdMetrics | None = None,
    ) -> tuple[list[TaskResult], BirdMetrics]:
        """Run multiple BIRD-Bench tasks with optional parallelism."""
        metrics = initial_metrics or BirdMetrics()
        results: list[TaskResult] = []
        output_dir = Path(output_dir)
        consecutive_errors = 0
        last_error: str | None = None

        semaphore = asyncio.Semaphore(self.config.parallel)

        async def run_with_semaphore(task: BirdTask) -> TaskResult:
            async with semaphore:
                return await self.run_task(task)

        def check_consecutive_errors(result: TaskResult) -> None:
            nonlocal consecutive_errors, last_error

            if result.status == TaskStatus.TASK_ERROR:
                consecutive_errors += 1
                last_error = result.error
                if consecutive_errors >= self.config.max_consecutive_errors:
                    raise EvalAbortedError(
                        f"Aborting: {consecutive_errors} consecutive task errors. "
                        f"Last error: {last_error}",
                        consecutive_errors,
                    )
            else:
                consecutive_errors = 0

        if self.config.parallel > 1:
            pending = [run_with_semaphore(task) for task in tasks]
            for coro in asyncio.as_completed(pending):
                result = await coro
                results.append(result)

                append_result(result, output_dir)
                is_correct = result.result.is_correct if result.result else False
                metrics.add_result(result, is_correct)
                update_overall(metrics, output_dir)

                check_consecutive_errors(result)

                if progress_callback:
                    progress_callback(len(results), len(tasks))
        else:
            for task in tasks:
                result = await self.run_task(task)
                results.append(result)

                append_result(result, output_dir)
                is_correct = result.result.is_correct if result.result else False
                metrics.add_result(result, is_correct)
                update_overall(metrics, output_dir)

                check_consecutive_errors(result)

                if progress_callback:
                    progress_callback(len(results), len(tasks))

        results.sort(key=lambda r: r.index)

        return results, metrics
