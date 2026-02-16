"""Remote file search via Daytona sandbox."""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ._utils import shell_quote
from .manager import SandboxManager

DEFAULT_MAX_RESULTS = 100


class DaytonaGlobHandler(ToolHandler):
    """Find files by name or path pattern in a remote Daytona sandbox.

    Standard agentic tool name: 'glob'
    """

    def __init__(self, manager: SandboxManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Find files by name or path pattern in the remote sandbox. Returns a list of matching file paths."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match file names (e.g., '*.py', '*.log', 'config.*', '**/*.yaml')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (absolute path)",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum files to return. Defaults to {DEFAULT_MAX_RESULTS}.",
                },
            },
            "required": ["pattern", "path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        glob_pattern = invocation.arguments.get("pattern", "")
        path_str = invocation.arguments.get("path", "")
        max_results = invocation.arguments.get("max_results", DEFAULT_MAX_RESULTS)

        if not glob_pattern:
            return ToolOutput(content="No pattern provided", success=False)
        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        try:
            sandbox = await self._manager.get_sandbox()

            # Use find command (universally available)
            # Convert glob pattern to find -name/-path pattern
            # Exclude common non-content directories
            exclude = (
                r" -not -path '*/.git/*'"
                r" -not -path '*/node_modules/*'"
                r" -not -path '*/__pycache__/*'"
                r" -not -path '*/.venv/*'"
                r" -not -path '*/venv/*'"
            )

            # If pattern contains /, use -path; otherwise use -name
            if "/" in glob_pattern:
                match_flag = "-path"
                find_pattern = f"*/{glob_pattern}" if not glob_pattern.startswith("*") else glob_pattern
            else:
                match_flag = "-name"
                find_pattern = glob_pattern

            cmd = (
                f"find {shell_quote(path_str)} -type f"
                f" {match_flag} {shell_quote(find_pattern)}"
                f"{exclude}"
            )

            response = await sandbox.process.exec(cmd, timeout=30)

            if response.exit_code != 0:
                output = (response.result or "").strip()
                if "No such file" in output:
                    return ToolOutput(content=f"Directory not found: {path_str}", success=False)
                return ToolOutput(content=f"Find failed: {output or 'Unknown error'}", success=False)

            output = response.result.strip()
            if not output:
                return ToolOutput(
                    content="No files found matching pattern",
                    success=True,
                    metadata={"matches": 0},
                )

            lines = output.split("\n")
            total_found = len(lines)
            truncated = total_found > max_results
            if truncated:
                lines = lines[:max_results]

            # Convert to relative paths
            results = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith(path_str + "/"):
                    results.append(line[len(path_str) + 1 :])
                else:
                    results.append(line)

            result = "\n".join(results)

            if truncated:
                result += f"\n\n[Showing {max_results} of {total_found} files]"
            else:
                result += f"\n\n[{len(results)} files found]"

            return ToolOutput(
                content=result,
                success=True,
                metadata={"matches": len(results), "total": total_found},
            )

        except Exception as e:
            return ToolOutput(content=f"Error finding files: {e}", success=False)

