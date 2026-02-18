"""File writing handler with configurable restrictions.

Supports two modes:
- CREATE_ONLY: Can only create new files, blocks overwrites and sensitive paths
- FULL: Full write access for sandboxed container environments
"""

from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput
from .paths import is_path_sensitive


class WriteHandler(ToolHandler):
    """Write content to a file with configurable restrictions.

    Standard agentic tool name: 'write'

    Modes:
    - CREATE_ONLY: Can only create new files, cannot overwrite existing files.
                   Blocks sensitive paths. Requires approval.
    - FULL: Full write access. Can create and overwrite files.
            No path restrictions. No approval required (container is sandbox).
    """

    def __init__(
        self,
        create_only: bool = True,
        requires_approval: bool | None = None,
    ):
        """Initialize WriteHandler.

        Args:
            create_only: If True, only allow creating new files (default).
                        If False, allow overwriting existing files.
            requires_approval: Override whether approval is required.
                              Defaults to True for create_only, False otherwise.
        """
        self._create_only = create_only
        self._requires_approval = (
            requires_approval if requires_approval is not None else create_only
        )

    @property
    def name(self) -> str:
        return "write"

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    @property
    def description(self) -> str:
        if self._create_only:
            return (
                "Write content to a new file. Use this when the user asks you to produce "
                "an output file such as a summary, report, script, or document. "
                "Cannot overwrite existing files."
            )
        else:
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
        path_str = invocation.arguments.get("path", "")
        content = invocation.arguments.get("content", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        if not content:
            return ToolOutput(content="No content provided", success=False)

        path = Path(path_str).expanduser().resolve()

        # Safety checks for create_only mode
        if self._create_only:
            # Check sensitive paths
            sensitive, reason = is_path_sensitive(path)
            if sensitive:
                return ToolOutput(content=reason, success=False)

            # Check if file already exists
            if path.exists():
                return ToolOutput(
                    content=f"File already exists: {path}. Use a different path or delete the existing file first.",
                    success=False,
                )

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

            if self._create_only:
                action = "Created"
            else:
                action = "Overwrote" if existed else "Created"

            return ToolOutput(
                content=f"{action} {path} ({size} bytes, {lines} lines)",
                success=True,
                metadata={
                    "path": str(path),
                    "size_bytes": size,
                    "lines": lines,
                    "overwrote": existed and not self._create_only,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)
