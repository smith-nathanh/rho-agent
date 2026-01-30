"""AgentBench evaluation tool handlers.

This module provides tool handlers specifically designed for AgentBench evaluation.
These handlers differ from the standard rho_agent.tools.handlers in several ways:

1. Tool Names: AgentBench expects specific tool names like 'execute_sql',
   'bash_action', 'commit_final_answer' - not our standard names.

2. Parameter Schemas: AgentBench uses simpler schemas (e.g., just {sql: string}
   for database queries instead of our operation-based interface).

3. Execution Context: Commands run inside Docker containers for isolation,
   not on the host system.

4. Eval-Specific Features: Output truncation, table hash calculation,
   answer capture via callbacks.

The handlers share code with standard handlers where possible (e.g., format_rows
from database.py) while maintaining AgentBench compatibility.

See runner.py for detailed documentation on why eval uses custom handlers.
"""

from typing import Callable

from rho_agent.tools.base import ToolHandler
from rho_agent.tools.registry import ToolRegistry

from .container_bash import ContainerBashHandler, ContainerProtocol
from .docker_shell import DockerShellHandler
from .submit_answer import FinishActionHandler, SubmitAnswerHandler
from .unrestricted_mysql import EvalMySQLHandler, UnrestrictedMySQLHandler
from .unrestricted_sqlite import EvalSqliteHandler, UnrestrictedSqliteHandler


def create_dbbench_registry(
    db_handler: ToolHandler,
    on_answer: Callable[[str], None],
) -> tuple[ToolRegistry, SubmitAnswerHandler]:
    """Create a tool registry for DBBench evaluation tasks.

    Args:
        db_handler: Database handler (EvalSqliteHandler or EvalMySQLHandler)
        on_answer: Callback when answer is submitted

    Returns:
        Tuple of (registry, submit_handler) so caller can check submission status
    """
    registry = ToolRegistry()
    registry.register(db_handler)

    submit_handler = SubmitAnswerHandler(
        tool_name="commit_final_answer",
        on_answer=on_answer,
    )
    registry.register(submit_handler)

    return registry, submit_handler


def create_os_registry(
    shell_handler: DockerShellHandler,
    on_answer: Callable[[str], None],
    on_finish: Callable[[], None],
) -> tuple[ToolRegistry, SubmitAnswerHandler, FinishActionHandler]:
    """Create a tool registry for OS Interaction evaluation tasks.

    Args:
        shell_handler: Docker shell handler for command execution
        on_answer: Callback when answer is submitted
        on_finish: Callback when task is finished

    Returns:
        Tuple of (registry, answer_handler, finish_handler)
    """
    registry = ToolRegistry()
    registry.register(shell_handler)

    answer_handler = SubmitAnswerHandler(
        tool_name="answer_action",
        on_answer=on_answer,
    )
    finish_handler = FinishActionHandler(on_finish=on_finish)
    registry.register(answer_handler)
    registry.register(finish_handler)

    return registry, answer_handler, finish_handler


__all__ = [
    # Handler classes
    "ContainerBashHandler",
    "ContainerProtocol",
    "DockerShellHandler",
    "EvalMySQLHandler",
    "EvalSqliteHandler",
    "FinishActionHandler",
    "SubmitAnswerHandler",
    # Backwards compatibility aliases
    "UnrestrictedMySQLHandler",
    "UnrestrictedSqliteHandler",
    # Factory functions
    "create_dbbench_registry",
    "create_os_registry",
]
