"""Submit answer tool for capturing agent's final answer."""

from collections.abc import Callable
from typing import Any

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput


class SubmitAnswerHandler(ToolHandler):
    """Tool for agents to submit their final answer.

    The tool name can be customized:
    - "commit_final_answer" for DBBench tasks
    - "answer_action" for OS interaction tasks

    When called, it captures the answer and signals that the task is complete.
    """

    def __init__(
        self,
        tool_name: str = "commit_final_answer",
        on_answer: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the submit answer handler.

        Args:
            tool_name: Name for this tool (e.g., "commit_final_answer" or "answer_action")
            on_answer: Callback to invoke when answer is submitted
        """
        self._tool_name = tool_name
        self._on_answer = on_answer
        self._submitted_answer: str | None = None
        self._is_submitted = False

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        if self._tool_name == "commit_final_answer":
            return (
                "Submit your final answer to the database question. "
                "Use this when you have determined the answer through SQL queries. "
                "The answer should be the exact value(s) that answer the question."
            )
        elif self._tool_name == "answer_action":
            return (
                "Submit your answer to the task. "
                "Use this when you have found the answer through shell commands. "
                "The answer should be exact (e.g., a number, filename, or single word)."
            )
        else:
            return "Submit your final answer to complete the task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "Your final answer to the question or task",
                },
            },
            "required": ["answer"],
        }

    @property
    def requires_approval(self) -> bool:
        return False

    @property
    def submitted_answer(self) -> str | None:
        """Get the submitted answer, if any."""
        return self._submitted_answer

    @property
    def is_submitted(self) -> bool:
        """Check if an answer has been submitted."""
        return self._is_submitted

    def reset(self) -> None:
        """Reset the handler state for a new task."""
        self._submitted_answer = None
        self._is_submitted = False

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Handle the answer submission."""
        answer = invocation.arguments.get("answer", "")

        if not answer:
            return ToolOutput(
                content="No answer provided. Please provide your answer.",
                success=False,
            )

        # Store the answer
        self._submitted_answer = str(answer)
        self._is_submitted = True

        # Call the callback if provided
        if self._on_answer:
            self._on_answer(self._submitted_answer)

        return ToolOutput(
            content=f"Answer submitted: {self._submitted_answer}",
            success=True,
            metadata={"answer": self._submitted_answer},
        )


class FinishActionHandler(ToolHandler):
    """Tool for agents to indicate task completion without a specific answer.

    Used for OS interaction tasks where the goal is to perform an action
    rather than find a specific answer.
    """

    def __init__(
        self,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the finish action handler.

        Args:
            on_finish: Callback to invoke when finish is called
        """
        self._on_finish = on_finish
        self._is_finished = False

    @property
    def name(self) -> str:
        return "finish_action"

    @property
    def description(self) -> str:
        return (
            "Indicate that you have completed the task. "
            "Use this when the task involves performing an action rather than "
            "finding a specific answer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Optional message describing what was done",
                },
            },
            "required": [],
        }

    @property
    def requires_approval(self) -> bool:
        return False

    @property
    def is_finished(self) -> bool:
        """Check if finish has been called."""
        return self._is_finished

    def reset(self) -> None:
        """Reset the handler state for a new task."""
        self._is_finished = False

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Handle the finish action."""
        message = invocation.arguments.get("message", "Task completed")

        self._is_finished = True

        if self._on_finish:
            self._on_finish()

        return ToolOutput(
            content=f"Task marked as complete: {message}",
            success=True,
        )
