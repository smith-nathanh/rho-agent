"""Bash tool that routes commands through a Docker container.

This tool is injected by the harness at runtime — the `environment` attribute
is set before the agent session starts. The meta-agent can modify this file
to change how commands are executed (add retries, output parsing, etc.).
"""

from __future__ import annotations

from typing import Any

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

# Maximum bytes of output to return to the LLM to avoid context overflow.
MAX_OUTPUT_BYTES = 30_000


class DockerBashHandler(ToolHandler):
    """Execute bash commands inside the task's Docker container."""

    # Set by the harness before the session starts.
    environment: Any = None
    timeout_sec: int = 120

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a bash command in the Linux container. "
            "Commands run as the default user with full access. "
            "Use this for all shell operations: running code, installing packages, "
            "reading files, building projects, etc."
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

        if self.environment is None:
            return ToolOutput(
                content="Error: no Docker environment available",
                success=False,
            )

        try:
            result = await self.environment.exec(
                command=command,
                timeout_sec=self.timeout_sec,
            )
        except Exception as e:
            return ToolOutput(content=f"Error executing command: {e}", success=False)

        # Combine stdout and stderr
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"STDERR:\n{result.stderr}")
        output = "\n".join(parts) if parts else "(no output)"

        # Truncate if too large
        if len(output.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
            # Keep beginning and end
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
