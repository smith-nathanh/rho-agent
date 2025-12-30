"""Search file contents handler - mirrors Claude Code's Grep tool."""

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

# Conservative defaults to avoid context overflow
DEFAULT_HEAD_LIMIT = 50
DEFAULT_MAX_FILES = 500
MAX_LINE_LENGTH = 500


def _truncate_line(text: str, max_len: int = MAX_LINE_LENGTH) -> str:
    """Truncate a line if it exceeds max length."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


class GrepFilesHandler(ToolHandler):
    """Search for patterns in files.

    Mirrors Claude Code's Grep tool with output_mode support:
    - files_with_matches (default): Just show which files match
    - content: Show matching lines with optional context
    - count: Show match counts per file
    """

    @property
    def name(self) -> str:
        return "grep_files"

    @property
    def description(self) -> str:
        return (
            "Search for a regex pattern in files. "
            "By default returns only file paths that match (output_mode='files_with_matches'). "
            "Use output_mode='content' to see matching lines, or 'count' for match counts. "
            "Use glob parameter to filter by file type (e.g., '*.py', '*.log')."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (absolute path)",
                },
                "glob": {
                    "type": "string",
                    "description": "File pattern to filter (e.g., '*.py', '*.log'). Defaults to all files.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Output mode: 'files_with_matches' (default, just paths), 'content' (matching lines), 'count' (match counts)",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search. Defaults to false.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before/after match (only for output_mode='content'). Defaults to 0.",
                },
                "head_limit": {
                    "type": "integer",
                    "description": f"Max results to return. Defaults to {DEFAULT_HEAD_LIMIT}.",
                },
            },
            "required": ["pattern", "path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        pattern_str = invocation.arguments.get("pattern", "")
        path_str = invocation.arguments.get("path", "")
        glob_pattern = invocation.arguments.get("glob", "*")
        output_mode = invocation.arguments.get("output_mode", "files_with_matches")
        ignore_case = invocation.arguments.get("ignore_case", False)
        context_lines = invocation.arguments.get("context_lines", 0)
        head_limit = invocation.arguments.get("head_limit", DEFAULT_HEAD_LIMIT)

        if not pattern_str:
            return ToolOutput(content="No pattern provided", success=False)

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"Path not found: {path}", success=False)

        # Compile regex
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern_str, flags)
        except re.error as e:
            return ToolOutput(content=f"Invalid regex pattern: {e}", success=False)

        # Collect files to search
        if path.is_file():
            files_to_search = [path]
        else:
            files_to_search = self._collect_files(path, glob_pattern)

        if not files_to_search:
            return ToolOutput(
                content=f"No files matching '{glob_pattern}' found in {path}",
                success=True,
            )

        # Search based on output mode
        if output_mode == "files_with_matches":
            return self._search_files_only(files_to_search, regex, head_limit)
        elif output_mode == "count":
            return self._search_with_counts(files_to_search, regex, head_limit)
        else:  # content
            return self._search_with_content(
                files_to_search, regex, context_lines, head_limit
            )

    def _collect_files(self, directory: Path, glob_pattern: str) -> list[Path]:
        """Collect files matching the glob pattern."""
        files = []
        skip_dirs = {".git", ".svn", "__pycache__", "node_modules", ".venv", "venv"}

        for root, dirs, filenames in directory.walk():
            # Skip hidden and common non-content directories
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

            for filename in filenames:
                if fnmatch.fnmatch(filename, glob_pattern):
                    files.append(root / filename)

                if len(files) >= DEFAULT_MAX_FILES:
                    return files

        return files

    def _search_files_only(
        self, files: list[Path], regex: re.Pattern, head_limit: int
    ) -> ToolOutput:
        """Return only file paths that contain matches."""
        matching_files = []

        for file_path in files:
            if len(matching_files) >= head_limit:
                break

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                    if regex.search(content):
                        matching_files.append(str(file_path))
            except (PermissionError, OSError):
                continue

        if not matching_files:
            return ToolOutput(
                content="No files match pattern",
                success=True,
                metadata={"files_searched": len(files), "matches": 0},
            )

        output = "\n".join(matching_files)
        total = len(matching_files)

        if total >= head_limit:
            output += f"\n\n[Showing {head_limit} of {total}+ matching files]"
        else:
            output += f"\n\n[{total} matching files]"

        return ToolOutput(
            content=output,
            success=True,
            metadata={"files_with_matches": total},
        )

    def _search_with_counts(
        self, files: list[Path], regex: re.Pattern, head_limit: int
    ) -> ToolOutput:
        """Return match counts per file."""
        results = []

        for file_path in files:
            if len(results) >= head_limit:
                break

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                    matches = len(regex.findall(content))
                    if matches > 0:
                        results.append((str(file_path), matches))
            except (PermissionError, OSError):
                continue

        if not results:
            return ToolOutput(
                content="No matches found",
                success=True,
                metadata={"files_searched": len(files), "matches": 0},
            )

        output_lines = [f"{count:6d}  {path}" for path, count in results]
        total_matches = sum(count for _, count in results)
        output_lines.append(f"\n[{total_matches} total matches in {len(results)} files]")

        return ToolOutput(
            content="\n".join(output_lines),
            success=True,
            metadata={"total_matches": total_matches, "files_with_matches": len(results)},
        )

    def _search_with_content(
        self, files: list[Path], regex: re.Pattern, context_lines: int, head_limit: int
    ) -> ToolOutput:
        """Return matching lines with optional context."""
        output_lines = []
        total_matches = 0
        files_with_matches = 0

        for file_path in files:
            if total_matches >= head_limit:
                break

            file_matches = self._search_file_content(
                file_path, regex, context_lines, head_limit - total_matches
            )

            if file_matches:
                files_with_matches += 1
                if output_lines:
                    output_lines.append("")  # Blank line between files
                output_lines.append(f"── {file_path} ──")
                output_lines.extend(file_matches)
                total_matches += len([line for line in file_matches if line.startswith(">")])

        if not output_lines:
            return ToolOutput(
                content="No matches found",
                success=True,
                metadata={"files_searched": len(files), "matches": 0},
            )

        content = "\n".join(output_lines)

        truncated = total_matches >= head_limit
        summary = f"\n[{total_matches} matches in {files_with_matches} files"
        if truncated:
            summary += ", truncated"
        summary += "]"
        content += summary

        return ToolOutput(
            content=content,
            success=True,
            metadata={
                "total_matches": total_matches,
                "files_with_matches": files_with_matches,
                "truncated": truncated,
            },
        )

    def _search_file_content(
        self, file_path: Path, regex: re.Pattern, context_lines: int, max_matches: int
    ) -> list[str]:
        """Search a single file and return formatted matching lines."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (PermissionError, OSError):
            return []

        output = []
        match_line_nums = set()

        # Find matching lines
        for i, line in enumerate(lines):
            if regex.search(line):
                match_line_nums.add(i)
                if len(match_line_nums) >= max_matches:
                    break

        # Format output with context
        shown_lines = set()
        for line_num in sorted(match_line_nums):
            # Context before
            for i in range(max(0, line_num - context_lines), line_num):
                if i not in shown_lines and i not in match_line_nums:
                    shown_lines.add(i)
                    output.append(f"  {i + 1:6d}  {_truncate_line(lines[i].rstrip())}")

            # The match itself
            if line_num not in shown_lines:
                shown_lines.add(line_num)
                output.append(f"> {line_num + 1:6d}  {_truncate_line(lines[line_num].rstrip())}")

            # Context after
            for i in range(line_num + 1, min(len(lines), line_num + context_lines + 1)):
                if i not in shown_lines and i not in match_line_nums:
                    shown_lines.add(i)
                    output.append(f"  {i + 1:6d}  {_truncate_line(lines[i].rstrip())}")

        return output
