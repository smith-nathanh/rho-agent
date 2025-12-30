"""Read file contents handler."""

from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

# Max lines to return by default to avoid overwhelming context
DEFAULT_MAX_LINES = 500


class ReadFileHandler(ToolHandler):
    """Read contents of a file with optional line range."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Use this to inspect source code, logs, "
            "config files, etc. Supports reading specific line ranges for large files."
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
        path_str = invocation.arguments.get("path", "")
        start_line = invocation.arguments.get("start_line", 1)
        end_line = invocation.arguments.get("end_line")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"File not found: {path}", success=False)

        if not path.is_file():
            return ToolOutput(content=f"Not a file: {path}", success=False)

        # Ensure start_line is at least 1
        start_line = max(1, start_line)

        # Default end_line if not provided
        if end_line is None:
            end_line = start_line + DEFAULT_MAX_LINES - 1

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)

            # Clamp to actual file bounds
            start_idx = start_line - 1  # Convert to 0-indexed
            end_idx = min(end_line, total_lines)

            if start_idx >= total_lines:
                return ToolOutput(
                    content=f"Start line {start_line} exceeds file length ({total_lines} lines)",
                    success=False,
                )

            selected_lines = lines[start_idx:end_idx]

            # Format with line numbers
            output_lines = []
            for i, line in enumerate(selected_lines, start=start_line):
                output_lines.append(f"{i:6d}  {line.rstrip()}")

            content = "\n".join(output_lines)

            # Add metadata about truncation
            if end_idx < total_lines:
                content += f"\n\n[Showing lines {start_line}-{end_idx} of {total_lines}]"

            return ToolOutput(
                content=content,
                success=True,
                metadata={
                    "path": str(path),
                    "start_line": start_line,
                    "end_line": end_idx,
                    "total_lines": total_lines,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error reading file: {e}", success=False)
