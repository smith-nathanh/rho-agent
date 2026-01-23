"""File writing for eval environments - allows create and overwrite.

SAFETY: Only use in sandboxed eval containers.
The container isolation provides security, not tool-level restrictions.
"""

from pathlib import Path
from typing import Any

from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput


class WriteFileHandler(ToolHandler):
    """Write content to a file, creating or overwriting as needed.

    Unlike the read-only WriteOutputHandler, this allows:
    - Overwriting existing files
    - Writing to any path in the container
    - No approval required

    The security boundary is the container, not the tool.
    """

    @property
    def name(self) -> str:
        return "write_file"

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
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # Container is the sandbox

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        content = invocation.arguments.get("content", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        path = Path(path_str).expanduser().resolve()

        try:
            # Create parent directories
            path.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            existed = path.exists()
            path.write_text(content, encoding="utf-8")

            size = len(content.encode("utf-8"))
            lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )
            action = "Overwrote" if existed else "Created"

            return ToolOutput(
                content=f"{action} {path} ({size} bytes, {lines} lines)",
                success=True,
                metadata={
                    "path": str(path),
                    "size": size,
                    "lines": lines,
                    "overwrote": existed,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)
