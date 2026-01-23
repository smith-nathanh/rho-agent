"""Unrestricted bash execution for eval environments.

SAFETY: Only use in sandboxed eval containers.
The container isolation provides security, not tool-level restrictions.
"""

import asyncio
from typing import Any

from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

DEFAULT_TIMEOUT = 300  # 5 minutes for complex builds


class BashHandler(ToolHandler):
    """Execute any bash command without restrictions.

    Unlike the read-only ShellHandler, this allows all operations:
    - Package installation (pip, apt, npm)
    - File operations (rm, mv, cp, mkdir)
    - Build systems (make, cmake, cargo)
    - Service management
    - Any shell command

    The security boundary is the container, not the tool.
    """

    def __init__(self, working_dir: str = "/app", timeout: int = DEFAULT_TIMEOUT):
        self._working_dir = working_dir
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a bash command. Use for running programs, installing packages, "
            "building code, file operations, and any other shell tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": f"Working directory (default: {self._working_dir})",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default: {DEFAULT_TIMEOUT})",
                },
            },
            "required": ["command"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # Container is the sandbox

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._working_dir)
        timeout = invocation.arguments.get("timeout", self._timeout)

        if not command:
            return ToolOutput(content="No command provided", success=False)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolOutput(
                    content=f"Command timed out after {timeout}s",
                    success=False,
                    metadata={"timed_out": True, "exit_code": -1},
                )

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Combine output
            parts = []
            if stdout_str:
                parts.append(stdout_str)
            if stderr_str:
                parts.append(f"[stderr]\n{stderr_str}")

            content = "\n".join(parts) if parts else "(no output)"

            return ToolOutput(
                content=content,
                success=process.returncode == 0,
                metadata={"exit_code": process.returncode},
            )

        except FileNotFoundError:
            return ToolOutput(
                content=f"Working directory not found: {working_dir}",
                success=False,
            )
        except Exception as e:
            return ToolOutput(content=f"Error: {e}", success=False)
