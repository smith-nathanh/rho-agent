"""Output truncation utilities to prevent context bloat."""

from __future__ import annotations

# Approximate bytes-per-token heuristic
APPROX_BYTES_PER_TOKEN = 4

# Default max tokens
MAX_OUTPUT_TOKENS = 5000


def truncate_output(
    content: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> str:
    """Truncate tool output to prevent context overflow.

    Uses token-based truncation:
    - Adds "Total output lines: N" header when truncated
    - Uses "...N tokens truncated..." marker
    - 50/50 head+tail split

    Args:
        content: The output to truncate.
        max_tokens: Maximum tokens to keep (uses 4 bytes/token heuristic).

    Returns:
        Truncated content with elision notice, or original if under limit.
    """
    max_bytes = max_tokens * APPROX_BYTES_PER_TOKEN
    content_bytes = len(content.encode("utf-8"))

    if content_bytes <= max_bytes:
        return content

    # Calculate how much to keep (50/50 split)
    half_bytes = max_bytes // 2

    # Find character positions that correspond to byte boundaries
    # (handles multi-byte UTF-8 characters)
    head_chars = 0
    head_bytes = 0
    for char in content:
        char_bytes = len(char.encode("utf-8"))
        if head_bytes + char_bytes > half_bytes:
            break
        head_bytes += char_bytes
        head_chars += 1

    tail_chars = 0
    tail_bytes = 0
    for char in reversed(content):
        char_bytes = len(char.encode("utf-8"))
        if tail_bytes + char_bytes > half_bytes:
            break
        tail_bytes += char_bytes
        tail_chars += 1

    # Calculate tokens truncated
    elided_bytes = content_bytes - head_bytes - tail_bytes
    elided_tokens = elided_bytes // APPROX_BYTES_PER_TOKEN

    # Count total lines for header
    total_lines = content.count("\n") + 1

    # Build output
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars > 0 else ""
    marker = f"\n...{elided_tokens} tokens truncated...\n"

    return f"Total output lines: {total_lines}\n\n{head}{marker}{tail}"
