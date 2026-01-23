"""Surgical file editing for eval environments.

Uses a search-and-replace approach with fuzzy matching for robustness.
Inspired by Codex's apply_patch format but simplified for LLM ease-of-use.

SAFETY: Only use in sandboxed eval containers.
The container isolation provides security, not tool-level restrictions.
"""

from pathlib import Path
from typing import Any

from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput


class EditFileHandler(ToolHandler):
    """Make surgical edits to existing files using search-and-replace.

    The edit is atomic: if the search string isn't found (or isn't unique),
    the file is not modified and an error is returned.

    Supports fuzzy matching:
    1. Exact match
    2. Whitespace-normalized match (trailing whitespace ignored)
    3. Indentation-flexible match (leading whitespace normalized)
    """

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Make a surgical edit to a file by replacing a specific string with new content. "
            "The old_string must uniquely identify the location to edit. "
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

    @property
    def requires_approval(self) -> bool:
        return False  # Container is the sandbox

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        old_string = invocation.arguments.get("old_string", "")
        new_string = invocation.arguments.get("new_string", "")

        if not path_str:
            return ToolOutput(content="No path provided", success=False)
        if not old_string:
            return ToolOutput(content="No old_string provided", success=False)

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"File not found: {path}", success=False)
        if not path.is_file():
            return ToolOutput(content=f"Not a file: {path}", success=False)

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolOutput(content=f"Error reading file: {e}", success=False)

        # Try matching strategies in order
        new_content, match_info = self._apply_edit(content, old_string, new_string)

        if new_content is None:
            return ToolOutput(
                content=match_info,  # Error message
                success=False,
            )

        try:
            path.write_text(new_content, encoding="utf-8")
            return ToolOutput(
                content=f"Edited {path}: {match_info}",
                success=True,
                metadata={"path": str(path)},
            )
        except Exception as e:
            return ToolOutput(content=f"Error writing file: {e}", success=False)

    def _apply_edit(
        self, content: str, old_string: str, new_string: str
    ) -> tuple[str | None, str]:
        """Apply the edit with fuzzy matching.

        Returns:
            (new_content, message) - new_content is None on failure
        """
        # Strategy 1: Exact match
        count = content.count(old_string)
        if count == 1:
            return content.replace(old_string, new_string, 1), "exact match"
        elif count > 1:
            return (
                None,
                f"old_string appears {count} times (must be unique). Add more context.",
            )

        # Strategy 2: Whitespace-normalized match
        normalized_old = self._normalize_whitespace(old_string)
        matches = []

        # Find all potential matches by sliding window
        lines = content.split("\n")
        old_lines = old_string.split("\n")

        for i in range(len(lines) - len(old_lines) + 1):
            window = "\n".join(lines[i : i + len(old_lines)])
            if self._normalize_whitespace(window) == normalized_old:
                matches.append((i, window))

        if len(matches) == 1:
            _idx, matched = matches[0]
            new_content = content.replace(matched, new_string, 1)
            return new_content, "whitespace-normalized match"
        elif len(matches) > 1:
            return (
                None,
                f"Found {len(matches)} whitespace-normalized matches (must be unique)",
            )

        # Strategy 3: Flexible indentation match
        # Normalize all leading whitespace, then match
        indent_normalized_old = self._normalize_indentation(old_string)
        matches = []

        for i in range(len(lines) - len(old_lines) + 1):
            window = "\n".join(lines[i : i + len(old_lines)])
            if self._normalize_indentation(window) == indent_normalized_old:
                matches.append((i, window))

        if len(matches) == 1:
            _idx, matched = matches[0]
            # Preserve the original indentation when replacing
            new_content = content.replace(
                matched, self._reindent(new_string, matched), 1
            )
            return new_content, "indentation-flexible match"
        elif len(matches) > 1:
            return (
                None,
                f"Found {len(matches)} indentation-flexible matches (must be unique)",
            )

        # No match found
        return None, "old_string not found in file. Check for typos or add more context."

    def _normalize_whitespace(self, s: str) -> str:
        """Normalize trailing whitespace on each line."""
        return "\n".join(line.rstrip() for line in s.split("\n"))

    def _normalize_indentation(self, s: str) -> str:
        """Remove leading whitespace from each line."""
        return "\n".join(line.lstrip() for line in s.split("\n"))

    def _reindent(self, new_string: str, matched: str) -> str:
        """Apply the indentation from matched to new_string."""
        matched_lines = matched.split("\n")
        new_lines = new_string.split("\n")

        if not matched_lines:
            return new_string

        # Detect indentation of first line in matched
        first_indent = len(matched_lines[0]) - len(matched_lines[0].lstrip())
        base_indent = matched_lines[0][:first_indent]

        # Apply to new_string (preserve relative indentation)
        result = []
        for i, line in enumerate(new_lines):
            if i == 0:
                result.append(base_indent + line.lstrip())
            elif line.strip():  # Non-empty line
                result.append(base_indent + line.lstrip())
            else:
                result.append(line)  # Preserve empty lines

        return "\n".join(result)
