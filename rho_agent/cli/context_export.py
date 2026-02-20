"""Session history export utilities for connect workflows."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_val = item.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
                else:
                    parts.append(json.dumps(item, ensure_ascii=True))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def serialize_session(
    messages: list[dict[str, Any]],
    max_tool_result_chars: int = 500,
    max_tool_args_chars: int = 200,
) -> str:
    """Convert session messages to grep-friendly plain text."""
    if not messages:
        return "=== TURN 1 ===\n[empty]\n"

    lines: list[str] = []
    turn_number = 0

    for message in messages:
        role = str(message.get("role", "unknown"))
        if role == "user":
            turn_number += 1
            lines.append(f"=== TURN {turn_number} ===")
            lines.append(f"[user] {_stringify_content(message.get('content'))}")
            continue

        if turn_number == 0:
            turn_number = 1
            lines.append(f"=== TURN {turn_number} ===")

        if role == "assistant":
            content = _stringify_content(message.get("content"))
            if content:
                lines.append(f"[assistant] {content}")
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name", "unknown"))
                    args = _truncate(
                        _stringify_content(function.get("arguments", "")), max_tool_args_chars
                    )
                    if args:
                        lines.append(f"[tool:{name}] {args}")
                    else:
                        lines.append(f"[tool:{name}]")
            continue

        if role == "tool":
            result = _truncate(_stringify_content(message.get("content")), max_tool_result_chars)
            lines.append(f"[tool_result] {result}")
            continue

        lines.append(f"[{role}] {_stringify_content(message.get('content'))}")

    return "\n".join(lines).rstrip() + "\n"


def write_context_file(path: Path, messages: list[dict[str, Any]]) -> None:
    """Write session export atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = serialize_session(messages)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
        temp_file.write(data)
        temp_path = Path(temp_file.name)
    temp_path.replace(path)
