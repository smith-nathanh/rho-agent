"""Base class for executing bash commands in containers.

This provides shared container execution logic that can be used by different
container-based bash handlers (Docker, Podman, etc.).
"""

from abc import ABC, abstractmethod
from typing import Any, Protocol

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput


DEFAULT_TIMEOUT = 120  # seconds
DEFAULT_MAX_OUTPUT = 800  # AgentBench truncates at 800 chars


class ContainerProtocol(Protocol):
    """Protocol for container implementations."""

    async def execute(self, command: str, timeout: int) -> tuple[int, str, str]:
        """Execute command in container. Returns (exit_code, stdout, stderr)."""
        ...


class ContainerBashHandler(ToolHandler, ABC):
    """Base class for executing bash commands in containers.

    Provides common functionality for container-based shell execution:
    - Output truncation (configurable, defaults to 800 chars for AgentBench)
    - Timeout handling
    - Combined stdout/stderr output formatting
    - Error handling

    Subclasses must implement:
    - _get_container(): Return the container to execute in
    - name property: Return the tool name
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        """Initialize the container bash handler.

        Args:
            timeout: Command timeout in seconds
            max_output: Maximum output length before truncation
        """
        self._timeout = timeout
        self._max_output = max_output

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name - subclasses must implement."""
        ...

    @abstractmethod
    def _get_container(self) -> ContainerProtocol:
        """Get the container to execute commands in."""
        ...

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
        return False  # No approval needed for sandboxed eval tasks

    def _truncate_output(self, output: str) -> str:
        """Truncate output to max_output chars with message."""
        if len(output) > self._max_output:
            return output[: self._max_output - 50] + "\n[truncated because the output is too long]"
        return output

    def _format_output(self, stdout: str, stderr: str) -> str:
        """Format combined stdout/stderr output."""
        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")

        content = "\n".join(output_parts) if output_parts else "(no output)"
        return self._truncate_output(content)

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the shell command in the container."""
        command = invocation.arguments.get("command", "")

        if not command:
            return ToolOutput(content="No command provided", success=False)

        try:
            container = self._get_container()
            exit_code, stdout, stderr = await container.execute(command, timeout=self._timeout)

            content = self._format_output(stdout, stderr)

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
