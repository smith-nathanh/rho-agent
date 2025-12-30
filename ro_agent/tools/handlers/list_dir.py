"""List directory contents handler."""

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

# Max entries to return by default
DEFAULT_MAX_ENTRIES = 200


class ListDirHandler(ToolHandler):
    """List contents of a directory."""

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. Shows file names, sizes, and types. "
            "Use this to explore directory structures and find files."
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

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"Directory not found: {path}", success=False)

        if not path.is_dir():
            return ToolOutput(content=f"Not a directory: {path}", success=False)

        try:
            if recursive:
                content = self._list_recursive(path, show_hidden, max_depth)
            else:
                content = self._list_flat(path, show_hidden)

            return ToolOutput(
                content=content,
                success=True,
                metadata={"path": str(path), "recursive": recursive},
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error listing directory: {e}", success=False)

    def _list_flat(self, path: Path, show_hidden: bool) -> str:
        """List directory contents in a flat format similar to ls -la."""
        entries = []

        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if not show_hidden and entry.name.startswith("."):
                continue

            try:
                stat_info = entry.stat()
                size = stat_info.st_size
                mtime = datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M")
                mode = stat.filemode(stat_info.st_mode)

                if entry.is_dir():
                    name = f"{entry.name}/"
                    size_str = "-"
                elif entry.is_symlink():
                    target = os.readlink(entry)
                    name = f"{entry.name} -> {target}"
                    size_str = "-"
                else:
                    name = entry.name
                    size_str = self._format_size(size)

                entries.append(f"{mode}  {size_str:>8}  {mtime}  {name}")

            except (PermissionError, OSError):
                entries.append(f"??????????  ?         ?                 {entry.name}")

            if len(entries) >= DEFAULT_MAX_ENTRIES:
                entries.append(f"\n[Truncated at {DEFAULT_MAX_ENTRIES} entries]")
                break

        if not entries:
            return "(empty directory)"

        return "\n".join(entries)

    def _list_recursive(
        self, path: Path, show_hidden: bool, max_depth: int, prefix: str = "", depth: int = 0
    ) -> str:
        """List directory contents in a tree format."""
        if depth > max_depth:
            return ""

        lines = []
        entries = []

        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return f"{prefix}[permission denied]\n"

        # Filter hidden if needed
        if not show_hidden:
            entries = [e for e in entries if not e.name.startswith(".")]

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                if depth < max_depth:
                    subtree = self._list_recursive(
                        entry, show_hidden, max_depth, child_prefix, depth + 1
                    )
                    if subtree:
                        lines.append(subtree.rstrip("\n"))
            else:
                size_str = self._format_size(entry.stat().st_size) if entry.exists() else "?"
                lines.append(f"{prefix}{connector}{entry.name} ({size_str})")

            if len(lines) >= DEFAULT_MAX_ENTRIES:
                lines.append(f"{prefix}[... truncated]")
                break

        return "\n".join(lines)

    @staticmethod
    def _format_size(size: int) -> str:
        """Format file size in human-readable form."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"
