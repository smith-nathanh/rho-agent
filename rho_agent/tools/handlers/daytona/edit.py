"""Remote file editing via Daytona sandbox.

Downloads the file, applies the edit in memory using EditHandler's fuzzy
matching logic, then uploads the result.
"""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ..edit import EditHandler
from .manager import SandboxManager

# Shared instance for reusing _apply_edit() logic
_edit_logic = EditHandler()


class DaytonaEditHandler(ToolHandler):
    """Make surgical edits to files in a remote Daytona sandbox.

    Standard agentic tool name: 'edit'
    """

    def __init__(self, manager: SandboxManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Make a surgical edit to a file in the remote sandbox by replacing a specific "
            "string with new content. The old_string must uniquely identify the location to edit. "
            "Include enough context (surrounding lines) to make the match unique. "
            "For multiple edits to the same file, call this tool multiple times."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": (
                        "The exact string to find and replace. Must be unique in the file. "
                        "Include surrounding lines for context if needed."
                    ),
                },
                "new_string": {
                    "type": "string",
                    "description": "The string to replace old_string with",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        old_string = invocation.arguments.get("old_string", "")
        new_string = invocation.arguments.get("new_string", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)
        if not old_string:
            return ToolOutput(content="No old_string provided", success=False)

        try:
            sandbox = await self._manager.get_sandbox()

            # Download the file
            file_bytes = await sandbox.fs.download_file(path_str)
            content = file_bytes.decode("utf-8")

            # Apply edit using shared fuzzy match logic
            new_content, match_info = _edit_logic._apply_edit(content, old_string, new_string)

            if new_content is None:
                return ToolOutput(content=match_info, success=False)

            # Upload the modified file
            await sandbox.fs.upload_file(new_content.encode("utf-8"), path_str)

            return ToolOutput(
                content=f"Edited {path_str}: {match_info}",
                success=True,
                metadata={"path": path_str},
            )

        except Exception as e:
            error = str(e)
            if "not found" in error.lower() or "no such file" in error.lower():
                return ToolOutput(content=f"File not found: {path_str}", success=False)
            return ToolOutput(content=f"Error editing file: {e}", success=False)
