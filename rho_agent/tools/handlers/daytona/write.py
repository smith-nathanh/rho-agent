"""Remote file writing via Daytona sandbox."""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ._utils import shell_quote
from .manager import SandboxManager


class DaytonaWriteHandler(ToolHandler):
    """Write content to a file in a remote Daytona sandbox.

    Standard agentic tool name: 'write'
    """

    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file in the remote sandbox. Creates the file if it doesn't exist, "
            "or overwrites it if it does. Creates parent directories as needed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path where the file should be written",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Write content to a file in the remote sandbox."""
        path_str = invocation.arguments.get("path", "")
        content = invocation.arguments.get("content", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        if not content:
            return ToolOutput(content="No content provided", success=False)

        try:
            sandbox = await self._manager.get_sandbox()

            # Ensure parent directory exists
            parent = str(_posix_parent(path_str))
            if parent and parent != path_str:
                await sandbox.process.exec(f"mkdir -p {shell_quote(parent)}", timeout=10)

            # Upload file content
            content_bytes = content.encode("utf-8")
            await sandbox.fs.upload_file(content_bytes, path_str)

            size = len(content_bytes)
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            return ToolOutput(
                content=f"Created {path_str} ({size} bytes, {lines} lines)",
                success=True,
                metadata={
                    "path": path_str,
                    "size_bytes": size,
                    "lines": lines,
                },
            )

        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)


def _posix_parent(path: str) -> str:
    """Get parent directory of a POSIX path."""
    from pathlib import PurePosixPath

    return str(PurePosixPath(path).parent)
