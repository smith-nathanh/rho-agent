"""Evaluation runner for AgentBench tasks.

Why Eval Uses Custom Handlers (not standard ro_agent.tools.handlers)
====================================================================

AgentBench requires specific tool interfaces that differ from our standard handlers:

1. Tool Names: AgentBench expects 'execute_sql', 'bash_action', 'commit_final_answer',
   'answer_action', 'finish_action' - not our standard 'sqlite', 'bash', etc.

2. Parameter Schemas: AgentBench uses simple {sql: string} for database queries,
   not our operation-based interface {operation: 'query', sql: string}.

3. Execution Context: Commands run inside Docker containers (EvalContainer, MySQLContainer)
   for isolation, not on the host system.

4. Eval-Specific Features: Output truncation (800 chars), table hash calculation for
   mutation verification, answer capture via callbacks.

The eval handlers share code with standard handlers where possible (format_rows,
is_read_only_sql from database.py) while maintaining AgentBench compatibility.

See ro_agent/capabilities for the CapabilityProfile system that configures standard handlers.

Original AgentBench System Prompts (for reference)
==================================================

DBBench (from AgentBench/src/server/tasks/dbbench/task.py):
-----------------------------------------------------------
I will ask you a question, then you should help me operate a MySQL database with SQL to answer the question.
You have to explain the problem and your solution to me and write down your thoughts.
After thinking and explaining thoroughly, every round you can choose to operate or to answer with the two specific tools provided.
If you should execute a SQL query, use the `execute_sql` function, Your SQL should be in one line.
Every time you can only execute one SQL statement. I will only execute the statement in the first SQL code block. Every time you write a SQL, I will execute it for you and give you the output.
If you are done operating, and you want to commit your final answer, then use the `commit_final_answer` function.
DO NOT use this tool unless you are sure about your answer. I expect an accurate and correct answer.
Your answer should be accurate. Your answer must be exactly the same as the correct answer.
If the question is about modifying the database, then after done operation, your answer field can be anything.
If your response cannot match any pattern I mentioned earlier, you will be judged as FAIL immediately.
You should always use the tools provided to submit your answer. Be careful not to write it in the content field.
Your input will be raw MySQL response, you have to deal with it by yourself.

OS-Interaction (from AgentBench/src/server/tasks/os_interaction/task.py):
-------------------------------------------------------------------------
You are an assistant that will act like a person. I will play the role of a Linux (Ubuntu) operating system.
Your goal is to implement the operations required by me or answer the questions proposed by me.
For each of your turns, you should first think about what you should do, and then call exactly one of the provided tools according to the situation.
If you think the output is too long, I will truncate it. The truncated output is not complete. You have to deal with the truncating problem by yourself.
Attention, your bash code should not contain any input operation. Once again, you should use one tool in each turn, and should not respond without function calling.
Note that if you think the task has been finished, or there is some message missing to completely complete the task, you should respond with calling the function "finish_action", as no additional information will be provided.
Also, note that if you have gotten the answer to the question, you should call the "answer_action" tool instead of simply writing your answer in your response.
Your answers should be exact and precise (for example, a single number), do not answer with full sentences or phrases.
Always use a tool provided instead of simply responding with content.
"""

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

from ro_agent.capabilities import CapabilityProfile
from ro_agent.client.model import ModelClient
from ro_agent.core.agent import Agent
from ro_agent.core.session import Session
from ro_agent.tools.registry import ToolRegistry

# Document the capability profile that eval tasks conceptually use.
# We don't use ToolFactory because eval requires custom handlers with specific
# tool names and interfaces, but this documents the intended configuration.
EVAL_PROFILE = CapabilityProfile.eval()

from .cerebras_client import CerebrasClient
from .config import (
    DBBenchResult,
    EvalAbortedError,
    EvalConfig,
    EvalMetrics,
    OSResult,
    TaskResult,
    TaskStatus,
)
from .docker.container import EvalContainer
from .docker.mysql_container import MySQLContainer
from .evaluators.db_evaluator import DBBenchEvaluator
from .evaluators.os_evaluator import OSEvaluator
from .output import append_result, update_overall
from .tasks.dbbench import DBBenchTask, create_sqlite_from_tableinfo
from .tasks.os_interaction import OSTask
from .tools.docker_shell import DockerShellHandler
from .tools.submit_answer import SubmitAnswerHandler, FinishActionHandler
from .tools.unrestricted_mysql import UnrestrictedMySQLHandler
from .tools.unrestricted_sqlite import UnrestrictedSqliteHandler


# System prompts for different task types
DBBENCH_SYSTEM_PROMPT = """You will answer questions by querying a database with SQL.

Tools:
- `execute_sql`: Run a SQL query (one statement at a time)
- `commit_final_answer`: Submit your final answer

IMPORTANT: Never call `commit_final_answer` in the same turn as `execute_sql`. You must see the query results before committing your answer. You may call `execute_sql` multiple times to explore the data, but only call `commit_final_answer` alone after you have the answer.

Answer format:
- Return the value exactly as it appears in the query result
- Submit only the specific value(s) requested, not entire rows
- If the question asks for a single item, return one answer
- Preserve any units or formatting present in the data
- No results: submit "none"
- Modifications (INSERT/UPDATE/DELETE): submit "done" after completing
"""

OS_SYSTEM_PROMPT = """You will complete tasks in a Linux environment by executing shell commands.

Tools:
- `bash_action`: Execute a shell command (no interactive input)
- `answer_action`: Submit your answer
- `finish_action`: Signal task completion (when no answer is needed)

IMPORTANT: Never call `answer_action` in the same turn as `bash_action`. You must see the command output before submitting your answer. Run commands first, observe the results, then call `answer_action` alone in a separate turn.

Answer format:
- Be exact and precise: a number, filename, or single value
- Do not answer with full sentences
- Your answer must be an actual value, not a variable name or placeholder like "$output"
- Output may be truncated; adjust your approach if needed
"""


class EvalRunner:
    """Orchestrates running evaluation tasks through the agent."""

    def __init__(
        self,
        config: EvalConfig,
        scripts_dir: Path | str | None = None,
    ) -> None:
        """Initialize the evaluation runner.

        Args:
            config: Evaluation configuration
            scripts_dir: Optional path to check scripts directory
        """
        self.config = config
        self._scripts_dir = Path(scripts_dir) if scripts_dir else None
        self._db_evaluator = DBBenchEvaluator()
        self._os_evaluator = OSEvaluator(scripts_dir=scripts_dir)
        self._mysql_container: MySQLContainer | None = None

    async def _ensure_mysql(self) -> MySQLContainer:
        """Lazily start MySQL container on first use."""
        if self._mysql_container is None:
            self._mysql_container = MySQLContainer()
            await self._mysql_container.start()
        return self._mysql_container

    def _needs_mysql(self, task: DBBenchTask) -> bool:
        """Check if task requires MySQL for proper evaluation."""
        return (
            task.query_type in ("INSERT", "UPDATE", "DELETE")
            and task.answer_md5 is not None
        )

    def _is_cerebras(self) -> bool:
        """Check if we're using Cerebras based on base_url."""
        return self.config.base_url and "cerebras" in self.config.base_url.lower()

    def _create_client(self) -> ModelClient | CerebrasClient:
        """Create the appropriate client based on config.

        Uses native Cerebras SDK when base_url contains 'cerebras',
        otherwise uses the standard OpenAI-compatible ModelClient.
        """
        if self._is_cerebras():
            print(f"[DEBUG] Using CerebrasClient for model={self.config.model}", file=sys.stderr)
            return CerebrasClient(model=self.config.model)
        print(f"[DEBUG] Using ModelClient for model={self.config.model} base_url={self.config.base_url}", file=sys.stderr)
        return ModelClient(
            model=self.config.model,
            base_url=self.config.base_url,
            service_tier=self.config.service_tier,
        )

    async def cleanup(self) -> None:
        """Cleanup resources (call at end of eval run)."""
        if self._mysql_container:
            await self._mysql_container.cleanup()
            self._mysql_container = None

    async def run_dbbench_task(self, task: DBBenchTask) -> TaskResult:
        """Run a single DBBench task.

        Routes to MySQL for mutation queries with answer_md5, SQLite otherwise.

        Args:
            task: The DBBench task to run

        Returns:
            TaskResult with evaluation results
        """
        if self._needs_mysql(task):
            return await self._run_dbbench_task_mysql(task)
        return await self._run_dbbench_task_sqlite(task)

    async def _run_dbbench_task_sqlite(self, task: DBBenchTask) -> TaskResult:
        """Run a DBBench task using SQLite (for SELECT queries)."""
        db_path = None
        handler = None

        try:
            # Create temporary SQLite database
            db_path = create_sqlite_from_tableinfo(
                task.table_name,
                task.table_info,
            )

            # Create tool registry
            registry = ToolRegistry()
            handler = UnrestrictedSqliteHandler(db_path=db_path)
            registry.register(handler)

            # Create submit answer handler with answer capture
            submitted_answer: str | None = None

            def capture_answer(answer: str) -> None:
                nonlocal submitted_answer
                submitted_answer = answer

            submit_handler = SubmitAnswerHandler(
                tool_name="commit_final_answer",
                on_answer=capture_answer,
            )
            registry.register(submit_handler)

            # Create session and agent
            system_prompt = self._get_system_prompt("dbbench")
            session = Session(system_prompt=system_prompt)
            client = self._create_client()
            agent = Agent(
                session=session,
                registry=registry,
                client=client,
                auto_compact=False,  # Don't auto-compact during eval
            )

            # Run the task
            prompt = task.get_prompt()
            status = TaskStatus.COMPLETED
            turns = 0
            consecutive_timeouts = 0
            turn_timeout = 600 if self.config.service_tier == "flex" else 120  # longer for flex

            for turn in range(self.config.max_turns):
                turns += 1

                # Check if answer was submitted
                if submit_handler.is_submitted:
                    break

                # Run a turn with timeout
                try:
                    async with asyncio.timeout(turn_timeout):
                        if turn == 0:
                            # First turn with the task prompt
                            async for event in agent.run_turn(prompt):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when answer is submitted
                                if event.type == "tool_end" and submit_handler.is_submitted:
                                    agent.request_cancel()
                                    break
                        else:
                            # Subsequent turns - prompt agent to continue
                            async for event in agent.run_turn("Continue working on the task."):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when answer is submitted
                                if event.type == "tool_end" and submit_handler.is_submitted:
                                    agent.request_cancel()
                                    break

                    # Reset timeout counter on successful turn
                    consecutive_timeouts = 0

                    # Check for answer after turn
                    if submit_handler.is_submitted:
                        break

                except TimeoutError:
                    consecutive_timeouts += 1
                    print(f"[Task {task.index}] Turn {turn} timed out ({consecutive_timeouts}/3)", file=sys.stderr)
                    if consecutive_timeouts >= 3:
                        raise EvalAbortedError(
                            f"Aborting: 3 consecutive turn timeouts. API may be unresponsive.",
                            consecutive_timeouts,
                        )
                except Exception as e:
                    if "context" in str(e).lower():
                        status = TaskStatus.AGENT_CONTEXT_LIMIT
                    else:
                        status = TaskStatus.TASK_ERROR
                    break

            # Check if we hit turn limit
            if turns >= self.config.max_turns and not submit_handler.is_submitted:
                status = TaskStatus.TASK_LIMIT_REACHED

            # Evaluate the answer
            is_correct = False
            if submitted_answer is not None:
                is_correct = self._db_evaluator.compare_results(
                    submitted_answer,
                    task.expected_answer,
                    task.query_type,
                )

            # Build result
            result = TaskResult(
                index=task.index,
                status=status,
                history=session.history.copy(),
                time=TaskResult.create_time(),
                result=DBBenchResult(
                    is_correct=is_correct,
                    answer=submitted_answer,
                    ground_truth=task.expected_answer,
                    std_sql=task.ground_truth_sql,
                    type=task.query_type,
                ),
            )

            return result

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
            # Cleanup
            if handler:
                handler.close()
            if db_path and Path(db_path).exists():
                Path(db_path).unlink()

    async def _run_dbbench_task_mysql(self, task: DBBenchTask) -> TaskResult:
        """Run a DBBench mutation task using MySQL for hash-based evaluation."""
        handler = None
        db_name = f"eval_{task.index}_{uuid.uuid4().hex[:6]}"

        try:
            # Ensure MySQL container is running
            mysql = await self._ensure_mysql()

            # Create fresh database for this task
            await mysql.create_database(db_name)

            # Create tool registry with MySQL handler
            handler = UnrestrictedMySQLHandler(
                container_id=mysql.container_id,
                database=db_name,
                password=mysql.PASSWORD,
            )

            # Initialize table with data
            await self._init_mysql_table(handler, task)

            registry = ToolRegistry()
            registry.register(handler)

            # Create submit answer handler
            submitted_answer: str | None = None

            def capture_answer(answer: str) -> None:
                nonlocal submitted_answer
                submitted_answer = answer

            submit_handler = SubmitAnswerHandler(
                tool_name="commit_final_answer",
                on_answer=capture_answer,
            )
            registry.register(submit_handler)

            # Create session and agent
            system_prompt = self._get_system_prompt("dbbench")
            session = Session(system_prompt=system_prompt)
            client = self._create_client()
            agent = Agent(
                session=session,
                registry=registry,
                client=client,
                auto_compact=False,
            )

            # Run the task
            prompt = task.get_prompt()
            status = TaskStatus.COMPLETED
            turns = 0
            consecutive_timeouts = 0
            turn_timeout = 600 if self.config.service_tier == "flex" else 120  # longer for flex

            for turn in range(self.config.max_turns):
                turns += 1

                if submit_handler.is_submitted:
                    break

                try:
                    async with asyncio.timeout(turn_timeout):
                        if turn == 0:
                            async for event in agent.run_turn(prompt):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when answer is submitted
                                if event.type == "tool_end" and submit_handler.is_submitted:
                                    agent.request_cancel()
                                    break
                        else:
                            async for event in agent.run_turn("Continue working on the task."):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when answer is submitted
                                if event.type == "tool_end" and submit_handler.is_submitted:
                                    agent.request_cancel()
                                    break

                    consecutive_timeouts = 0

                    if submit_handler.is_submitted:
                        break

                except TimeoutError:
                    consecutive_timeouts += 1
                    print(f"[Task {task.index}] Turn {turn} timed out ({consecutive_timeouts}/3)", file=sys.stderr)
                    if consecutive_timeouts >= 3:
                        raise EvalAbortedError(
                            f"Aborting: 3 consecutive turn timeouts. API may be unresponsive.",
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

            # For mutations, calculate table hash and compare to answer_md5
            table_hash = await handler.calculate_table_hash(
                task.table_info.to_dict(),
                task.table_name,
            )
            is_correct = self._db_evaluator.compare_hash(table_hash, task.answer_md5)

            result = TaskResult(
                index=task.index,
                status=status,
                history=session.history.copy(),
                time=TaskResult.create_time(),
                result=DBBenchResult(
                    is_correct=is_correct,
                    answer=submitted_answer,
                    ground_truth=task.answer_md5,  # Store hash as ground truth for mutations
                    std_sql=task.ground_truth_sql,
                    type=task.query_type,
                ),
            )

            return result

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
            if handler:
                handler.close()
            if self._mysql_container:
                await self._mysql_container.drop_database(db_name)

    async def _init_mysql_table(self, handler: UnrestrictedMySQLHandler, task: DBBenchTask) -> None:
        """Initialize MySQL table with task data."""
        from ro_agent.tools.base import ToolInvocation

        # Build CREATE TABLE statement
        col_defs = []
        for col in task.table_info.columns:
            col_name = f"`{col['name']}`"
            col_type = col.get("type", "TEXT").upper()
            if col_type in ("STRING", "VARCHAR", "CHAR"):
                col_type = "TEXT"
            col_defs.append(f"{col_name} {col_type}")

        create_sql = f"CREATE TABLE `{task.table_name}` ({', '.join(col_defs)})"

        await handler.handle(ToolInvocation(
            call_id="init_create",
            tool_name="execute_sql",
            arguments={"sql": create_sql},
        ))

        # Insert rows in batches via docker exec
        if task.table_info.rows:
            col_names = ", ".join(f"`{col['name']}`" for col in task.table_info.columns)

            # Build INSERT with multiple VALUES for efficiency
            # Process in batches to avoid command line length limits
            batch_size = 100
            for i in range(0, len(task.table_info.rows), batch_size):
                batch = task.table_info.rows[i : i + batch_size]
                values_list = []
                for row in batch:
                    escaped_values = []
                    for val in row:
                        if val is None:
                            escaped_values.append("NULL")
                        elif isinstance(val, (int, float)):
                            escaped_values.append(str(val))
                        else:
                            # Escape single quotes for SQL
                            escaped = str(val).replace("\\", "\\\\").replace("'", "\\'")
                            escaped_values.append(f"'{escaped}'")
                    values_list.append(f"({', '.join(escaped_values)})")

                insert_sql = f"INSERT INTO `{task.table_name}` ({col_names}) VALUES {', '.join(values_list)}"

                await handler.handle(ToolInvocation(
                    call_id=f"init_insert_{i}",
                    tool_name="execute_sql",
                    arguments={"sql": insert_sql},
                ))

    async def run_os_task(self, task: OSTask) -> TaskResult:
        """Run a single OS Interaction task.

        Args:
            task: The OS task to run

        Returns:
            TaskResult with evaluation results
        """
        container = None

        try:
            # Start Docker container
            container = EvalContainer(image=task.image)
            await container.start()

            # Run init code/file
            if task.init_code:
                await container.run_init(task.init_code)
            if task.init_file:
                # Resolve init_file relative to scripts_dir
                init_path = task.init_file
                if task.scripts_dir:
                    init_path = str(Path(task.scripts_dir) / task.init_file)
                await container.run_init_file(init_path)

            # Run background start script
            if task.start_script:
                await container.run_background(task.start_script)

            # Create tool registry
            registry = ToolRegistry()
            shell_handler = DockerShellHandler(container=container)
            registry.register(shell_handler)

            # Create submit answer handler
            submitted_answer: str | None = None
            finished = False

            def capture_answer(answer: str) -> None:
                nonlocal submitted_answer
                submitted_answer = answer

            def on_finish() -> None:
                nonlocal finished
                finished = True

            answer_handler = SubmitAnswerHandler(
                tool_name="answer_action",
                on_answer=capture_answer,
            )
            finish_handler = FinishActionHandler(on_finish=on_finish)
            registry.register(answer_handler)
            registry.register(finish_handler)

            # Create session and agent
            system_prompt = self._get_system_prompt("os")
            session = Session(system_prompt=system_prompt)
            client = self._create_client()
            agent = Agent(
                session=session,
                registry=registry,
                client=client,
                auto_compact=False,
            )

            # Run the task
            prompt = task.get_prompt()
            status = TaskStatus.COMPLETED
            turns = 0
            consecutive_timeouts = 0
            turn_timeout = 600 if self.config.service_tier == "flex" else 120  # longer for flex

            for turn in range(self.config.max_turns):
                turns += 1

                # Check if done
                if answer_handler.is_submitted or finish_handler.is_finished:
                    break

                # Run a turn with timeout
                try:
                    async with asyncio.timeout(turn_timeout):
                        if turn == 0:
                            async for event in agent.run_turn(prompt):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when task is done
                                if event.type == "tool_end" and (answer_handler.is_submitted or finish_handler.is_finished):
                                    agent.request_cancel()
                                    break
                        else:
                            async for event in agent.run_turn("Continue working on the task."):
                                if event.type == "error":
                                    print(f"[Task {task.index}] API Error: {event.content}", file=sys.stderr)
                                # Stop agent loop immediately when task is done
                                if event.type == "tool_end" and (answer_handler.is_submitted or finish_handler.is_finished):
                                    agent.request_cancel()
                                    break

                    consecutive_timeouts = 0

                    if answer_handler.is_submitted or finish_handler.is_finished:
                        break

                except TimeoutError:
                    consecutive_timeouts += 1
                    print(f"[Task {task.index}] Turn {turn} timed out ({consecutive_timeouts}/3)", file=sys.stderr)
                    if consecutive_timeouts >= 3:
                        raise EvalAbortedError(
                            f"Aborting: 3 consecutive turn timeouts. API may be unresponsive.",
                            consecutive_timeouts,
                        )
                except Exception as e:
                    if "context" in str(e).lower():
                        status = TaskStatus.AGENT_CONTEXT_LIMIT
                    else:
                        status = TaskStatus.TASK_ERROR
                    break

            # Check turn limit
            if turns >= self.config.max_turns and not (
                answer_handler.is_submitted or finish_handler.is_finished
            ):
                status = TaskStatus.TASK_LIMIT_REACHED

            # Evaluate
            is_correct = await self._os_evaluator.evaluate(
                submitted_answer,
                task.evaluation,
                container,
                scripts_dir=task.scripts_dir,
            )

            result = TaskResult(
                index=task.index,
                status=status,
                history=session.history.copy(),
                time=TaskResult.create_time(),
                result=OSResult(result=is_correct),
            )

            return result

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
            # Cleanup container
            if container:
                await container.cleanup()

    def _get_system_prompt(self, task_type: str) -> str:
        """Get the system prompt for a task type.

        If a custom prompt file is configured, load it. Otherwise use defaults.
        """
        if self.config.system_prompt_file:
            try:
                return Path(self.config.system_prompt_file).read_text()
            except Exception:
                pass  # Fall back to default

        if task_type == "dbbench":
            return DBBENCH_SYSTEM_PROMPT
        elif task_type == "os":
            return OS_SYSTEM_PROMPT
        else:
            return DBBENCH_SYSTEM_PROMPT

    async def run_dbbench_tasks(
        self,
        tasks: list[DBBenchTask],
        output_dir: Path | str,
        progress_callback: Any = None,
    ) -> tuple[list[TaskResult], EvalMetrics]:
        """Run multiple DBBench tasks with optional parallelism.

        Args:
            tasks: List of tasks to run
            output_dir: Directory for incremental result saving
            progress_callback: Optional callback(completed, total) for progress

        Returns:
            Tuple of (results list, aggregate metrics)

        Raises:
            EvalAbortedError: If max_consecutive_errors task errors occur in a row
        """
        metrics = EvalMetrics()
        results: list[TaskResult] = []
        output_dir = Path(output_dir)
        consecutive_errors = 0
        last_error: str | None = None

        # Create semaphore for parallelism
        semaphore = asyncio.Semaphore(self.config.parallel)

        async def run_with_semaphore(task: DBBenchTask) -> TaskResult:
            async with semaphore:
                return await self.run_dbbench_task(task)

        def check_consecutive_errors(result: TaskResult) -> None:
            """Check for consecutive errors and raise if threshold exceeded."""
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

        try:
            # Run tasks
            if self.config.parallel > 1:
                # Parallel execution
                pending = [run_with_semaphore(task) for task in tasks]
                for coro in asyncio.as_completed(pending):
                    result = await coro
                    results.append(result)

                    # Save incrementally
                    append_result(result, output_dir)

                    # Update metrics
                    is_correct = (
                        result.result.is_correct
                        if isinstance(result.result, DBBenchResult)
                        else False
                    )
                    metrics.add_result(result, is_correct)
                    update_overall(metrics, output_dir)

                    # Check for consecutive errors (less reliable in parallel mode)
                    check_consecutive_errors(result)

                    if progress_callback:
                        progress_callback(len(results), len(tasks))
            else:
                # Sequential execution
                for task in tasks:
                    result = await self.run_dbbench_task(task)
                    results.append(result)

                    # Save incrementally
                    append_result(result, output_dir)

                    is_correct = (
                        result.result.is_correct
                        if isinstance(result.result, DBBenchResult)
                        else False
                    )
                    metrics.add_result(result, is_correct)
                    update_overall(metrics, output_dir)

                    # Check for consecutive errors
                    check_consecutive_errors(result)

                    if progress_callback:
                        progress_callback(len(results), len(tasks))

            # Sort by index
            results.sort(key=lambda r: r.index)

        finally:
            # Cleanup MySQL container if it was started
            await self.cleanup()

        return results, metrics

    async def run_os_tasks(
        self,
        tasks: list[OSTask],
        output_dir: Path | str,
        progress_callback: Any = None,
    ) -> tuple[list[TaskResult], EvalMetrics]:
        """Run multiple OS tasks with optional parallelism.

        Args:
            tasks: List of tasks to run
            output_dir: Directory for incremental result saving
            progress_callback: Optional callback(completed, total) for progress

        Returns:
            Tuple of (results list, aggregate metrics)

        Raises:
            EvalAbortedError: If max_consecutive_errors task errors occur in a row
        """
        metrics = EvalMetrics()
        results: list[TaskResult] = []
        output_dir = Path(output_dir)
        consecutive_errors = 0
        last_error: str | None = None

        semaphore = asyncio.Semaphore(self.config.parallel)

        async def run_with_semaphore(task: OSTask) -> TaskResult:
            async with semaphore:
                return await self.run_os_task(task)

        def check_consecutive_errors(result: TaskResult) -> None:
            """Check for consecutive errors and raise if threshold exceeded."""
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

                # Save incrementally
                append_result(result, output_dir)

                is_correct = (
                    result.result.result
                    if isinstance(result.result, OSResult)
                    else False
                )
                metrics.add_result(result, is_correct)
                update_overall(metrics, output_dir)

                # Check for consecutive errors (less reliable in parallel mode)
                check_consecutive_errors(result)

                if progress_callback:
                    progress_callback(len(results), len(tasks))
        else:
            for task in tasks:
                result = await self.run_os_task(task)
                results.append(result)

                # Save incrementally
                append_result(result, output_dir)

                is_correct = (
                    result.result.result
                    if isinstance(result.result, OSResult)
                    else False
                )
                metrics.add_result(result, is_correct)
                update_overall(metrics, output_dir)

                # Check for consecutive errors
                check_consecutive_errors(result)

                if progress_callback:
                    progress_callback(len(results), len(tasks))

        results.sort(key=lambda r: r.index)

        return results, metrics
