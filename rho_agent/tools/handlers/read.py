"""Read file contents handler."""

from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

# Max lines to return by default to avoid overwhelming context
DEFAULT_MAX_LINES = 500
# Max characters per line to avoid huge single-line payloads
MAX_LINE_LENGTH = 500

# Binary file extensions to reject
BINARY_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".tiff", ".tif",
    # Audio/Video
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac", ".ogg", ".webm",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    # Compiled/Binary
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".pyc", ".pyo", ".class", ".wasm",
    # Documents (binary formats)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Other binary
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
}


class ReadHandler(ToolHandler):
    """Read contents of a file with optional line range.

    Standard agentic tool name: 'read'
    """

    @property
    def name(self) -> str:
        return "read"

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

        # Check for binary files
        suffix = path.suffix.lower()
        if suffix in BINARY_EXTENSIONS:
            return ToolOutput(
                content=f"Cannot read binary file: {path} ({suffix} files are not text-readable). Use shell commands like 'file', 'exiftool', or 'strings' for binary inspection.",
                success=False,
            )

        # Ensure start_line is at least 1
        start_line = max(1, start_line)

        # Default end_line if not provided
        if end_line is None:
            end_line = start_line + DEFAULT_MAX_LINES - 1

        try:
            output_lines = []
            total_lines = 0

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, start=1):
                    total_lines = line_no
                    if line_no < start_line:
                        continue
                    if line_no > end_line:
                        break
                    formatted = line.rstrip()
                    if len(formatted) > MAX_LINE_LENGTH:
                        formatted = formatted[:MAX_LINE_LENGTH] + "..."
                    output_lines.append(f"{line_no:6d}  {formatted}")

            if total_lines < start_line:
                return ToolOutput(
                    content=f"Start line {start_line} exceeds file length ({total_lines} lines)",
                    success=False,
                )

            end_idx = min(end_line, total_lines)

            content = "\n".join(output_lines)

            # Add metadata about truncation
            if end_idx < total_lines:
                content += (
                    f"\n\n[Showing lines {start_line}-{end_idx} of {total_lines}]"
                )

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
