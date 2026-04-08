"""DockerBashHandler — routes bash commands through a Harbor DockerEnvironment.

The harness creates one of these per scenario, sets `environment` to the
running DockerEnvironment, and registers it on the agent. Workspace tools/
may define their own bash handler which takes precedence (same tool name),
but this serves as the reliable fallback.
"""

from __future__ import annotations

from typing import Any

from ...tools.base import ToolHandler, ToolInvocation, ToolOutput

MAX_OUTPUT_BYTES = 30_000


class DockerBashHandler(ToolHandler):
    """Execute bash commands inside a Harbor Docker container."""

    def __init__(self, environment: Any, timeout_sec: int = 120) -> None:
        self._environment = environment
        self._timeout_sec = timeout_sec

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a bash command in the Linux container. "
            "Commands run as the default user with full access."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
            },
            "required": ["command"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        command = invocation.arguments.get("command", "")
        if not command.strip():
            return ToolOutput(content="Error: empty command", success=False)

        try:
            result = await self._environment.exec(
                command=command,
                timeout_sec=self._timeout_sec,
            )
        except Exception as e:
            return ToolOutput(content=f"Error executing command: {e}", success=False)

        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"STDERR:\n{result.stderr}")
        output = "\n".join(parts) if parts else "(no output)"

        # Truncate if too large
        if len(output.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
            half = MAX_OUTPUT_BYTES // 2
            output_bytes = output.encode("utf-8", errors="replace")
            output = (
                output_bytes[:half].decode("utf-8", errors="replace")
                + f"\n\n[... {len(output_bytes) - MAX_OUTPUT_BYTES} bytes elided ...]\n\n"
                + output_bytes[-half:].decode("utf-8", errors="replace")
            )

        if result.return_code != 0:
            output = f"Exit code: {result.return_code}\n{output}"

        return ToolOutput(
            content=output,
            success=result.return_code == 0,
            metadata={"return_code": result.return_code},
        )
