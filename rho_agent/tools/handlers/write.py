"""Write a file, creating or overwriting it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import ToolHandler, ToolInvocation, ToolOutput
from .paths import resolve_path, write_refusal

if TYPE_CHECKING:
    from ...sandbox import Sandbox


class WriteHandler(ToolHandler):
    """Write content to a file. Standard agentic tool name: 'write'.

    With a sandbox, paths outside its writable directories are refused, or,
    when a user can answer, they are asked to allow the file's directory.
    """

    def __init__(self, working_dir: str | None = None, sandbox: Sandbox | None = None):
        """
        Args:
            working_dir: Directory relative paths are resolved against.
                Defaults to the process's current directory.
            sandbox: The access policy to check paths against; None allows any path.
        """
        self._working_dir = working_dir
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file if it doesn't exist, "
            "or overwrites it if it does. Creates parent directories as needed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path where the file should be written (relative paths are taken from the working directory)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Write content to a file."""
        path_str = invocation.arguments.get("path", "")
        content = invocation.arguments.get("content", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        if not content:
            return ToolOutput(content="No content provided", success=False)

        path = resolve_path(path_str, self._working_dir)
        refusal = await write_refusal(self._sandbox, invocation, path, "write")
        if refusal:
            return ToolOutput(content=refusal, success=False)

        try:
            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            # Track if we're overwriting
            existed = path.exists()

            # Write the file
            path.write_text(content, encoding="utf-8")

            # Report success with file info
            size = len(content.encode("utf-8"))
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            action = "Overwrote" if existed else "Created"

            return ToolOutput(
                content=f"{action} {path} ({size} bytes, {lines} lines)",
                success=True,
                metadata={
                    "path": str(path),
                    "size_bytes": size,
                    "lines": lines,
                    "overwrote": existed,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)
