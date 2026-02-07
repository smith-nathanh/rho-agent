"""Find files by name or path pattern using ripgrep."""

import asyncio
import shutil
from pathlib import Path
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

DEFAULT_MAX_RESULTS = 100
DEFAULT_TIMEOUT = 30  # seconds


class GlobHandler(ToolHandler):
    """Find files by name or path pattern using ripgrep.

    Standard agentic tool name: 'glob'
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self._timeout = timeout
        self._rg_path = shutil.which("rg")

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Find files by name or path pattern. Returns a list of matching file paths."

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
        if not self._rg_path:
            return ToolOutput(
                content="ripgrep (rg) is not installed. Install it with: brew install ripgrep (macOS) or apt install ripgrep (Linux)",
                success=False,
            )

        glob_pattern = invocation.arguments.get("pattern", "")
        path_str = invocation.arguments.get("path", "")
        max_results = invocation.arguments.get("max_results", DEFAULT_MAX_RESULTS)

        if not glob_pattern:
            return ToolOutput(content="No pattern provided", success=False)

        if not path_str:
            return ToolOutput(content="No path provided", success=False)

        path = Path(path_str).expanduser().resolve()

        if not path.exists():
            return ToolOutput(content=f"Directory not found: {path}", success=False)

        if not path.is_dir():
            return ToolOutput(content=f"Not a directory: {path}", success=False)

        # Build rg command
        cmd = [self._rg_path, "--files"]

        # Glob pattern for file matching
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

        # Search path
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

            # rg --files returns exit code 1 for "no matches" (not an error)
            if process.returncode not in (0, 1):
                error = stderr.decode("utf-8", errors="replace").strip()
                return ToolOutput(
                    content=f"Find failed: {error}",
                    success=False,
                )

            output = stdout.decode("utf-8", errors="replace")

            if not output.strip():
                return ToolOutput(
                    content="No files found matching pattern",
                    success=True,
                    metadata={"matches": 0},
                )

            lines = output.strip().split("\n")
            total_found = len(lines)

            # Truncate to max_results
            truncated = total_found > max_results
            if truncated:
                lines = lines[:max_results]

            # Convert to relative paths
            results = []
            for line in lines:
                try:
                    rel_path = Path(line).relative_to(path)
                    results.append(str(rel_path))
                except ValueError:
                    results.append(line)

            result = "\n".join(results)

            if truncated:
                result += f"\n\n[Showing {max_results} of {total_found} files]"
            else:
                result += f"\n\n[{total_found} files found]"

            return ToolOutput(
                content=result,
                success=True,
                metadata={"matches": len(results), "total": total_found},
            )

        except Exception as e:
            return ToolOutput(content=f"Error finding files: {e}", success=False)
