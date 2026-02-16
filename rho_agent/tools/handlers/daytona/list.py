"""Remote directory listing via Daytona sandbox."""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ._utils import shell_quote
from .manager import SandboxManager


class DaytonaListHandler(ToolHandler):
    """List directory contents in a remote Daytona sandbox.

    Standard agentic tool name: 'list'
    """

    def __init__(self, manager: SandboxManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "list"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory in the remote sandbox. "
            "Shows file names, sizes, and modification times."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the directory to list",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files (starting with '.'). Defaults to false.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively (tree view). Defaults to false.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max depth for recursive listing. Defaults to 3.",
                },
            },
            "required": ["path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        show_hidden = invocation.arguments.get("show_hidden", False)
        recursive = invocation.arguments.get("recursive", False)
        max_depth = invocation.arguments.get("max_depth", 3)

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        try:
            sandbox = await self._manager.get_sandbox()

            if recursive:
                # Use find for recursive listing (tree may not be installed)
                hidden_filter = "" if show_hidden else r" -not -name '.*' -not -path '*/.*'"
                cmd = (
                    f"find {shell_quote(path_str)} -maxdepth {max_depth}"
                    f"{hidden_filter}"
                    " -printf '%M  %8s  %TY-%Tm-%Td %TH:%TM  %p\\n'"
                )
            else:
                hidden_flag = "-la" if show_hidden else "-l"
                cmd = f"ls {hidden_flag} {shell_quote(path_str)}"

            response = await sandbox.process.exec(cmd, timeout=30)

            if response.exit_code != 0:
                error = response.result or "Unknown error"
                if "No such file" in error or "not found" in error.lower():
                    return ToolOutput(content=f"Directory not found: {path_str}", success=False)
                if "Not a directory" in error:
                    return ToolOutput(content=f"Not a directory: {path_str}", success=False)
                return ToolOutput(content=f"Error listing directory: {error}", success=False)

            output = (response.result or "").strip()
            if not output:
                return ToolOutput(
                    content="(empty directory)",
                    success=True,
                    metadata={"path": path_str, "recursive": recursive, "item_count": 0},
                )

            # Count items (skip the "total" line from ls -l)
            lines = output.split("\n")
            if recursive and len(lines) > 200:
                lines = lines[:200]
                output = "\n".join(lines) + "\n\n[Showing first 200 entries]"
            item_count = sum(1 for line in lines if line and not line.startswith("total "))

            return ToolOutput(
                content=output,
                success=True,
                metadata={"path": path_str, "recursive": recursive, "item_count": item_count},
            )

        except Exception as e:
            return ToolOutput(content=f"Error listing directory: {e}", success=False)
