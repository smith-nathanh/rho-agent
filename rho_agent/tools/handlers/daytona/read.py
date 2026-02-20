"""Remote file reading via Daytona sandbox."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ..read import BINARY_EXTENSIONS
from ._utils import shell_quote
from .manager import SandboxManager

DEFAULT_MAX_LINES = 500
MAX_LINE_LENGTH = 500


class DaytonaReadHandler(ToolHandler):
    """Read file contents from a remote Daytona sandbox.

    Standard agentic tool name: 'read'
    """

    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file in the remote sandbox. Use this to inspect source code, "
            "logs, config files, etc. Supports reading specific line ranges for large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-indexed, inclusive). Defaults to 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read (1-indexed, inclusive). Defaults to start_line + 500.",
                },
            },
            "required": ["path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Read file contents from the remote sandbox."""
        path_str = invocation.arguments.get("path", "")
        start_line = invocation.arguments.get("start_line", 1)
        end_line = invocation.arguments.get("end_line")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        # Check for binary files
        suffix = PurePosixPath(path_str).suffix.lower()
        if suffix in BINARY_EXTENSIONS:
            return ToolOutput(
                content=f"Cannot read binary file: {path_str} ({suffix} files are not text-readable). "
                "Use bash with 'file', 'strings', or similar for binary inspection.",
                success=False,
            )

        start_line = max(1, int(start_line))
        if end_line is None:
            end_line = start_line + DEFAULT_MAX_LINES - 1
        else:
            end_line = int(end_line)

        try:
            sandbox = await self._manager.get_sandbox()

            # Use sed for line ranges to avoid downloading entire large files
            response = await sandbox.process.exec(
                f"sed -n '{start_line},{end_line}p' {shell_quote(path_str)} && "
                f"wc -l < {shell_quote(path_str)}",
                timeout=30,
            )

            if response.exit_code != 0:
                error = response.result or "Unknown error"
                if "No such file" in error:
                    return ToolOutput(content=f"File not found: {path_str}", success=False)
                return ToolOutput(content=f"Error reading file: {error}", success=False)

            output = response.result or ""

            # The last line of output is the total line count from wc -l
            lines = output.split("\n")
            # wc -l output is the last non-empty line
            total_lines = 0
            content_lines = lines
            for i in range(len(lines) - 1, -1, -1):
                stripped = lines[i].strip()
                if stripped.isdigit():
                    total_lines = int(stripped)
                    content_lines = lines[:i]
                    break

            if total_lines < start_line:
                return ToolOutput(
                    content=f"Start line {start_line} exceeds file length ({total_lines} lines)",
                    success=False,
                )

            # Format with line numbers, matching local ReadHandler output
            output_lines = []
            for idx, line in enumerate(content_lines):
                line_no = start_line + idx
                if line_no > end_line:
                    break
                formatted = line.rstrip()
                if len(formatted) > MAX_LINE_LENGTH:
                    formatted = formatted[:MAX_LINE_LENGTH] + "..."
                output_lines.append(f"{line_no:6d}  {formatted}")

            end_idx = min(end_line, start_line + len(content_lines) - 1)
            if total_lines > 0:
                end_idx = min(end_idx, total_lines)

            content = "\n".join(output_lines)

            if total_lines > 0 and end_idx < total_lines:
                content += f"\n\n[Showing lines {start_line}-{end_idx} of {total_lines}]"

            return ToolOutput(
                content=content,
                success=True,
                metadata={
                    "path": path_str,
                    "start_line": start_line,
                    "end_line": end_idx,
                    "total_lines": total_lines,
                },
            )

        except Exception as e:
            return ToolOutput(content=f"Error reading file: {e}", success=False)
