"""Write output file handler - allows agent to produce output files on request."""

from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput


class WriteOutputHandler(ToolHandler):
    """Write content to a file.

    This tool allows the agent to produce output files (summaries, reports,
    scripts, etc.) when explicitly requested by the user. Unlike the rest
    of ro-agent which is read-only, this tool exists specifically to export
    research findings.
    """

    @property
    def name(self) -> str:
        return "write_output"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Use this when the user asks you to produce "
            "an output file such as a summary, report, script, or document. "
            "The file will be created or overwritten at the specified path."
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

    @property
    def requires_approval(self) -> bool:
        return True

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        content = invocation.arguments.get("content", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        if not content:
            return ToolOutput(content="No content provided", success=False)

        path = Path(path_str).expanduser().resolve()

        # Safety check: don't overwrite certain sensitive files
        sensitive_patterns = [
            ".bashrc", ".zshrc", ".profile", ".bash_profile",
            ".ssh/", ".gnupg/", ".aws/", ".config/",
            "/etc/", "/usr/", "/bin/", "/sbin/",
        ]
        path_str_lower = str(path).lower()
        for pattern in sensitive_patterns:
            if pattern in path_str_lower:
                return ToolOutput(
                    content=f"Cannot write to sensitive location: {path}",
                    success=False,
                )

        # Check if file already exists
        if path.exists():
            return ToolOutput(
                content=f"File already exists: {path}. Use a different path or delete the existing file first.",
                success=False,
            )

        try:
            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            # Write the file
            path.write_text(content, encoding="utf-8")

            # Report success with file info
            size = len(content.encode("utf-8"))
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            return ToolOutput(
                content=f"Written {size} bytes ({lines} lines) to {path}",
                success=True,
                metadata={
                    "path": str(path),
                    "size_bytes": size,
                    "lines": lines,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)
