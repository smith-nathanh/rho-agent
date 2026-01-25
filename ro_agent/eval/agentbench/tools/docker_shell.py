"""Docker shell handler for OS interaction evaluation tasks.

This handler executes bash commands inside a Docker container (EvalContainer)
for full isolation. Key differences from standard BashHandler:

1. Tool name: 'bash_action' (AgentBench) vs 'bash' (standard)
2. Execution: Inside EvalContainer (Docker) vs on host system
3. Output: 800 char truncation (AgentBench spec)
4. No approval required (sandboxed environment)

See runner.py for why eval uses custom handlers.
"""

from typing import TYPE_CHECKING

from .container_bash import ContainerBashHandler, ContainerProtocol

if TYPE_CHECKING:
    from ..docker.container import EvalContainer


DEFAULT_TIMEOUT = 120  # seconds
MAX_OUTPUT_LENGTH = 800  # AgentBench truncates at 800 chars


class DockerShellHandler(ContainerBashHandler):
    """Shell handler that executes commands inside a Docker container.

    Used for OS interaction evaluation where commands need to run
    in an isolated Docker environment (EvalContainer).
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
        super().__init__(timeout=timeout, max_output=MAX_OUTPUT_LENGTH)
        self._container = container

    @property
    def name(self) -> str:
        return "bash_action"

    def _get_container(self) -> ContainerProtocol:
        """Return the EvalContainer to execute commands in."""
        return self._container
