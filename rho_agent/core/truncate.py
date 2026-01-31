"""Output truncation with file persistence for debugging."""

import hashlib
from pathlib import Path

MAX_TOOL_OUTPUT_CHARS = 20000
OUTPUT_PERSIST_DIR = Path("/tmp/rho-outputs")


def truncate_output(
    content: str,
    max_chars: int = MAX_TOOL_OUTPUT_CHARS,
    tool_name: str = "tool",
    persist_dir: Path | None = OUTPUT_PERSIST_DIR,
) -> str:
    """Truncate tool output to prevent context overflow.

    Uses head+tail strategy: keeps the first half and last half of the
    budget, so error messages at the end of output are preserved.

    When truncating, saves the full output to a file for debugging.

    Args:
        content: The output to truncate.
        max_chars: Maximum characters to keep.
        tool_name: Name of the tool (used in persisted filename).
        persist_dir: Directory to save full output (None to disable).

    Returns:
        Truncated content with elision notice, or original if under limit.
    """
    if len(content) <= max_chars:
        return content

    half = max_chars // 2
    elided = len(content) - max_chars

    # Persist full output for debugging
    file_path = None
    if persist_dir:
        persist_dir.mkdir(parents=True, exist_ok=True)
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        file_path = persist_dir / f"{tool_name}_{content_hash}.txt"
        if not file_path.exists():
            file_path.write_text(content)

    # Calculate approximate line number at truncation point
    head_lines = content[:half].count("\n") + 1

    # Build elision notice with actionable guidance
    notice = f"\n\n[OUTPUT TRUNCATED: {elided} chars elided around line {head_lines}]"
    if file_path:
        notice += f"\nFull output: {file_path}"
    notice += "\nTip: Filter with grep/head/tail, or redirect to file and search.\n\n"

    return content[:half] + notice + content[-half:]
