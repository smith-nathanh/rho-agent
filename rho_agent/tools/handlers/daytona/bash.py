"""Remote shell execution via Daytona sandbox."""

from __future__ import annotations

import json
import time
from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from .manager import SandboxManager


class DaytonaBashHandler(ToolHandler):
    """Execute shell commands in a remote Daytona sandbox.

    Standard agentic tool name: 'bash'
    """

    def __init__(self, manager: SandboxManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a bash command in a remote cloud sandbox. Use for running programs, "
            "installing packages, building code, file operations, and any other shell tasks."
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
                "working_dir": {
                    "type": "string",
                    "description": f"Working directory (default: {self._manager.working_dir})",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 300)",
                },
            },
            "required": ["command"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._manager.working_dir)
        timeout = invocation.arguments.get("timeout", 300)

        if not command:
            return ToolOutput(content="No command provided", success=False)

        start_time = time.perf_counter()

        try:
            sandbox = await self._manager.get_sandbox()
            response = await sandbox.process.exec(
                command,
                cwd=working_dir,
                timeout=timeout,
            )

            duration = time.perf_counter() - start_time
            output = response.result or ""

            content = json.dumps(
                {
                    "output": output,
                    "metadata": {
                        "exit_code": response.exit_code,
                        "duration_seconds": round(duration, 1),
                    },
                },
                indent=2,
            )

            return ToolOutput(
                content=content,
                success=response.exit_code == 0,
                metadata={
                    "exit_code": response.exit_code,
                    "duration_seconds": round(duration, 1),
                    "command": command,
                    "working_dir": working_dir,
                },
            )

        except Exception as e:
            duration = time.perf_counter() - start_time
            # Check for Daytona-specific errors
            error_name = type(e).__name__
            if "Timeout" in error_name:
                msg = f"Command timed out after {timeout}s"
            else:
                msg = f"Sandbox execution error: {e}"

            content = json.dumps(
                {
                    "output": msg,
                    "metadata": {
                        "exit_code": -1,
                        "duration_seconds": round(duration, 1),
                    },
                },
                indent=2,
            )

            return ToolOutput(
                content=content,
                success=False,
                metadata={
                    "exit_code": -1,
                    "duration_seconds": round(duration, 1),
                    "command": command,
                    "working_dir": working_dir,
                },
            )
