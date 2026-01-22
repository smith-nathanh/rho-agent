"""Docker shell handler for OS interaction evaluation tasks."""

from typing import Any, TYPE_CHECKING

from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

if TYPE_CHECKING:
    from ..docker.container import EvalContainer


DEFAULT_TIMEOUT = 120  # seconds
MAX_OUTPUT_LENGTH = 800  # AgentBench truncates at 800 chars


class DockerShellHandler(ToolHandler):
    """Shell handler that executes commands inside a Docker container.

    Used for OS interaction evaluation where commands need to run
    in an isolated Docker environment.
    """

    def __init__(
        self,
        container: "EvalContainer",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the Docker shell handler.

        Args:
            container: The EvalContainer to execute commands in
            timeout: Command timeout in seconds
        """
        self._container = container
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "bash_action"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the Linux environment. "
            "You can run any command to investigate the system, install packages, "
            "manipulate files, or perform any shell operation needed to complete the task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # No approval needed for eval tasks

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the shell command in the Docker container."""
        command = invocation.arguments.get("command", "")

        if not command:
            return ToolOutput(content="No command provided", success=False)

        try:
            exit_code, stdout, stderr = await self._container.execute(
                command, timeout=self._timeout
            )

            # Combine output
            output_parts = []
            if stdout:
                output_parts.append(stdout)
            if stderr:
                output_parts.append(f"[stderr]\n{stderr}")

            content = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate long output (matches AgentBench behavior)
            if len(content) > MAX_OUTPUT_LENGTH:
                content = content[:MAX_OUTPUT_LENGTH - 50] + "\n[truncated because the output is too long]"

            return ToolOutput(
                content=content,
                success=exit_code == 0,
                metadata={
                    "exit_code": exit_code,
                    "command": command,
                },
            )

        except TimeoutError:
            return ToolOutput(
                content=f"Command timed out after {self._timeout} seconds",
                success=False,
                metadata={"timed_out": True},
            )
        except Exception as e:
            return ToolOutput(
                content=f"Error executing command in container: {e}",
                success=False,
            )
