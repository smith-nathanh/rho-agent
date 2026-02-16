"""Remote content search via Daytona sandbox."""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ._utils import shell_quote
from .manager import SandboxManager

DEFAULT_MAX_MATCHES = 100
DEFAULT_CONTEXT_LINES = 0


class DaytonaGrepHandler(ToolHandler):
    """Search for patterns in files in a remote Daytona sandbox.

    Standard agentic tool name: 'grep'
    """

    def __init__(self, manager: SandboxManager):
        self._manager = manager
        self._has_rg: bool | None = None  # Cached after first check

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for a pattern in file contents in the remote sandbox. "
            "Returns matching lines with file paths and line numbers."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (absolute path)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter which files to search (e.g., '*.py', '*.log')",
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

        try:
            sandbox = await self._manager.get_sandbox()

            # Try rg first (faster), fall back to grep
            cmd = await self._build_command(
                sandbox, pattern, path_str, glob_pattern,
                ignore_case, context_lines, max_matches,
            )

            response = await sandbox.process.exec(cmd, timeout=30)

            # Both grep and rg return exit code 1 for "no matches"
            if response.exit_code not in (0, 1):
                error = response.result or "Unknown error"
                return ToolOutput(content=f"Search failed: {error}", success=False)

            output = (response.result or "").strip()
            if not output:
                return ToolOutput(
                    content="No matches found",
                    success=True,
                    metadata={"matches": 0},
                )

            # Count matches and truncate
            lines = output.split("\n")
            match_count = 0
            result_lines = []

            for line in lines:
                if not line:
                    result_lines.append(line)
                    continue

                # Match lines contain file:line:content pattern
                is_match = ":" in line and not _is_context_line(line)
                if is_match:
                    match_count += 1
                    if match_count > max_matches:
                        break

                result_lines.append(line)

            truncated = match_count > max_matches
            result = "\n".join(result_lines)

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
            return ToolOutput(content=f"Search error: {e}", success=False)

    async def _build_command(
        self,
        sandbox,
        pattern: str,
        path: str,
        glob_pattern: str | None,
        ignore_case: bool,
        context_lines: int,
        max_matches: int,
    ) -> str:
        """Build the search command, preferring rg if available."""
        # Check if rg is available (cached after first call)
        if self._has_rg is None:
            check = await sandbox.process.exec("which rg 2>/dev/null", timeout=5)
            self._has_rg = check.exit_code == 0
        has_rg = self._has_rg

        if has_rg:
            return self._build_rg_command(
                pattern, path, glob_pattern, ignore_case, context_lines, max_matches
            )
        return self._build_grep_command(
            pattern, path, glob_pattern, ignore_case, context_lines, max_matches
        )

    def _build_rg_command(
        self, pattern, path, glob_pattern, ignore_case, context_lines, max_matches
    ) -> str:
        parts = ["rg", "--line-number", "--with-filename", "--no-heading", "--color=never"]

        if ignore_case:
            parts.append("--ignore-case")
        if context_lines > 0:
            parts.extend(["--context", str(context_lines)])
        if glob_pattern:
            parts.extend(["--glob", shell_quote(glob_pattern)])

        # Exclude common directories
        for exclude in [".git/", "node_modules/", "__pycache__/", ".venv/", "venv/"]:
            parts.extend(["--glob", f"'!{exclude}'"])

        parts.append(shell_quote(pattern))
        parts.append(shell_quote(path))

        return " ".join(parts)

    def _build_grep_command(
        self, pattern, path, glob_pattern, ignore_case, context_lines, max_matches
    ) -> str:
        parts = ["grep", "-rn", "--color=never"]

        if ignore_case:
            parts.append("-i")
        if context_lines > 0:
            parts.extend(["-C", str(context_lines)])
        if glob_pattern:
            parts.extend(["--include", shell_quote(glob_pattern)])

        # Exclude common directories
        for exclude in [".git", "node_modules", "__pycache__", ".venv", "venv"]:
            parts.extend(["--exclude-dir", shell_quote(exclude)])

        parts.append(shell_quote(pattern))
        parts.append(shell_quote(path))

        # Limit output
        parts.append(f"| head -n {max_matches * 3}")

        return " ".join(parts)


def _is_context_line(line: str) -> bool:
    """Check if a line is a context line (uses - separator) vs match (uses :)."""
    first_colon = line.find(":")
    if first_colon == -1:
        return True
    rest = line[first_colon + 1 :]
    dash_pos = rest.find("-")
    colon_pos = rest.find(":")
    if dash_pos == -1 and colon_pos == -1:
        return True
    if dash_pos == -1:
        return False
    if colon_pos == -1:
        return True
    return dash_pos < colon_pos


