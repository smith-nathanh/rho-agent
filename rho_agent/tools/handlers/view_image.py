"""Show an image file to the model.

Chat-completion tool results are text only, so the image itself travels in a
user message that the session adds right after the turn's tool results (see
``Session._flush_images``). This handler returns the encoded image on
``ToolOutput.images`` and a short text result.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..base import ToolHandler, ToolInvocation, ToolOutput
from .paths import read_refusal, resolve_path

if TYPE_CHECKING:
    from ...sandbox import Sandbox

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Larger files are scaled down when Pillow is installed, and refused otherwise.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Longest edge after scaling down.
MAX_IMAGE_EDGE = 1568


def is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_MEDIA_TYPES


def _shrink(data: bytes) -> tuple[bytes, str, int] | None:
    """Scale an oversized image down to JPEG, trying smaller sizes until it fits.

    Returns (bytes, media type, longest edge), or None when Pillow is
    unavailable, the file is not a readable image, or nothing fits.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            original = source.convert("RGB")
        for edge, quality in ((MAX_IMAGE_EDGE, 85), (1024, 80), (768, 70), (512, 60)):
            img = original.copy()
            img.thumbnail((edge, edge))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality)
            if out.tell() <= MAX_IMAGE_BYTES:
                return out.getvalue(), "image/jpeg", max(img.size)
    except Exception:
        return None
    return None


def encode_image(path: str, data: bytes) -> ToolOutput:
    """Build the tool result for image bytes read from ``path``."""
    media_type = IMAGE_MEDIA_TYPES.get(Path(path).suffix.lower())
    if media_type is None:
        supported = ", ".join(sorted(IMAGE_MEDIA_TYPES))
        return ToolOutput(
            content=f"Not a supported image type: {path}. Supported: {supported}.", success=False
        )
    if not data:
        return ToolOutput(content=f"Image file is empty: {path}", success=False)

    note = ""
    if len(data) > MAX_IMAGE_BYTES:
        shrunk = _shrink(data)
        if shrunk is None:
            limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
            return ToolOutput(
                content=(
                    f"Image is too large to attach: {path} is {len(data) / 1e6:.1f} MB and the "
                    f"limit is {limit_mb} MB. Make a smaller copy first, for example with "
                    f"ImageMagick or Python, and view that."
                ),
                success=False,
            )
        data, media_type, edge = shrunk
        note = f" (scaled down to fit, longest edge {edge}px)"

    encoded = base64.b64encode(data).decode("ascii")
    return ToolOutput(
        content=f"Image attached: {path}{note}. It follows in the next message.",
        metadata={"image": True, "media_type": media_type, "bytes": len(data)},
        images=[{"path": path, "media_type": media_type, "data": encoded}],
    )


class ViewImageHandler(ToolHandler):
    """Attach a local image file so the model can look at it."""

    def __init__(self, working_dir: str | None = None, sandbox: Sandbox | None = None) -> None:
        self._working_dir = working_dir
        self._sandbox = sandbox

    @property
    def parallel_safe(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "view_image"

    @property
    def description(self) -> str:
        return (
            "Look at an image file (PNG, JPEG, GIF or WebP), such as a screenshot, a plot or "
            "a camera frame. The image is attached to the conversation so you can see it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the image file"},
            },
            "required": ["path"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        path_str = invocation.arguments.get("path", "")
        if not path_str:
            return ToolOutput(content="No path provided", success=False)
        path = resolve_path(path_str, self._working_dir)

        refusal = read_refusal(self._sandbox, invocation, path)
        if refusal:
            return ToolOutput(content=refusal, success=False)
        if not path.exists():
            return ToolOutput(content=f"File not found: {path}", success=False)
        if not path.is_file():
            return ToolOutput(content=f"Not a file: {path}", success=False)
        try:
            data = path.read_bytes()
        except OSError as e:
            return ToolOutput(content=f"Error reading image: {e}", success=False)
        return encode_image(str(path), data)
