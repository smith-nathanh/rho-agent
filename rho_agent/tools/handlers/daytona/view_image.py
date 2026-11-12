"""Show an image file from the remote sandbox to the model."""

from __future__ import annotations

from typing import Any

from ...base import ToolHandler, ToolInvocation, ToolOutput
from ..view_image import ViewImageHandler, encode_image
from .manager import SandboxManager


class DaytonaViewImageHandler(ToolHandler):
    """Download an image from the sandbox and attach it."""

    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager
        self._local = ViewImageHandler()

    @property
    def parallel_safe(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return self._local.name

    @property
    def description(self) -> str:
        return self._local.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._local.parameters

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        if not path_str:
            return ToolOutput(content="No path provided", success=False)
        try:
            sandbox = await self._manager.get_sandbox()
            data = await sandbox.fs.download_file(path_str)
        except Exception as e:
            return ToolOutput(content=f"Error reading image {path_str}: {e}", success=False)
        return encode_image(path_str, bytes(data or b""))
