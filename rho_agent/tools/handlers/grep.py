"""Search file contents using ripgrep."""

import asyncio
import shutil
from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

# Limits to prevent context overflow
DEFAULT_MAX_MATCHES = 100
DEFAULT_CONTEXT_LINES = 0
DEFAULT_TIMEOUT = 30  # seconds


class GrepHandler(ToolHandler):
    """Search for patterns in files using ripgrep.

    Standard agentic tool name: 'grep'

    Uses `rg` under the hood for efficient streaming search without
    loading files into memory. Suitable for searching large log files.
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self._timeout = timeout
        self._rg_path = shutil.which("rg")

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for a pattern in file contents. Returns matching lines "
            "with file paths and line numbers."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for in file contents (e.g., 'ERROR', 'connection failed', 'error|warning', 'def \\w+\\(')",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (absolute path)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter which files to search (e.g., '*.py', '*.log', '*.yaml', 'test_*.py')",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search. Defaults to false.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before and after each match. Defaults to 0.",
                },
                "max_matches": {
                    "type": "integer",
                    "description": f"Maximum total matches to return. Defaults to {DEFAULT_MAX_MATCHES}.",
                },
            },
            "required": ["pattern", "path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        # Check if rg is available
        if not self._rg_path:
            return ToolOutput(
                content="ripgrep (rg) is not installed. Install it with: brew install ripgrep (macOS) or apt install ripgrep (Linux)",
                success=False,
            )

        pattern = invocation.arguments.get("pattern", "")
        path_str = invocation.arguments.get("path", "")
        glob_pattern = invocation.arguments.get("glob")
        ignore_case = invocation.arguments.get("ignore_case", False)
        context_lines = invocation.arguments.get("context_lines", DEFAULT_CONTEXT_LINES)
        max_matches = invocation.arguments.get("max_matches", DEFAULT_MAX_MATCHES)

        if not pattern:
            return ToolOutput(content="No pattern provided", success=False)

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"Path not found: {path}", success=False)

        # Build rg command
        cmd = [self._rg_path]

        # Always include line numbers and filename
        cmd.extend(["--line-number", "--with-filename"])

        # No heading (file:line:content format)
        cmd.append("--no-heading")

        # Color off for clean parsing
        cmd.append("--color=never")

        # Case insensitive
        if ignore_case:
            cmd.append("--ignore-case")

        # Context lines
        if context_lines > 0:
            cmd.extend(["--context", str(context_lines)])

        # Glob filter (only for directories)
        if glob_pattern and path.is_dir():
            cmd.extend(["--glob", glob_pattern])

        # Skip common non-content directories
        cmd.extend(
            [
                "--glob",
                "!.git/",
                "--glob",
                "!node_modules/",
                "--glob",
                "!__pycache__/",
                "--glob",
                "!.venv/",
                "--glob",
                "!venv/",
            ]
        )

        # The pattern and path
        cmd.append(pattern)
        cmd.append(str(path))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._timeout,
                )
            except asyncio.CancelledError:
                # Clean up subprocess on cancellation
                process.kill()
                await process.wait()
                raise
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolOutput(
                    content=f"Search timed out after {self._timeout} seconds",
                    success=False,
                )

            # rg returns exit code 1 for "no matches" (not an error)
            if process.returncode not in (0, 1):
                error = stderr.decode("utf-8", errors="replace").strip()
                return ToolOutput(
                    content=f"Search failed: {error}",
                    success=False,
                )

            output = stdout.decode("utf-8", errors="replace")

            if not output.strip():
                return ToolOutput(
                    content="No matches found",
                    success=True,
                    metadata={"matches": 0},
                )

            # Truncate to max_matches lines (approximate, as context adds lines)
            lines = output.split("\n")

            # Count actual matches (lines that look like file:line:content, not context)
            match_count = 0
            truncated = False
            result_lines = []

            for line in lines:
                if not line:
                    result_lines.append(line)
                    continue

                # Context lines from rg start with file:line- (dash instead of colon)
                # Match lines are file:line:content
                is_match = ":" in line and not self._is_context_line(line)

                if is_match:
                    match_count += 1
                    if match_count > max_matches:
                        truncated = True
                        break

                result_lines.append(line)

            result = "\n".join(result_lines)

            # Add summary
            if truncated:
                result += f"\n\n[Showing {max_matches} of {match_count}+ matches, truncated]"
            else:
                result += f"\n\n[{match_count} matches]"

            return ToolOutput(
                content=result,
                success=True,
                metadata={
                    "matches": min(match_count, max_matches),
                    "truncated": truncated,
                },
            )

        except Exception as e:
            return ToolOutput(
                content=f"Search error: {e}",
                success=False,
            )

    def _is_context_line(self, line: str) -> bool:
        """Check if a line is a context line (uses - separator) vs match (uses :)."""
        # rg format: filename:linenum:content for matches
        # rg format: filename-linenum-content for context
        # We need to check if the second separator is - or :

        # Find first colon (end of filename on Windows could have drive letter)
        first_colon = line.find(":")
        if first_colon == -1:
            return True  # Not a standard rg line

        # After filename, look for linenum separator
        rest = line[first_colon + 1 :]

        # Find where the line number ends
        dash_pos = rest.find("-")
        colon_pos = rest.find(":")

        if dash_pos == -1 and colon_pos == -1:
            return True
        if dash_pos == -1:
            return False  # Only colon found, it's a match
        if colon_pos == -1:
            return True  # Only dash found, it's context

        # Both found - whichever comes first after the digits determines type
        return dash_pos < colon_pos
