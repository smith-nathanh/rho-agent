"""Tool registry for storing and dispatching to handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from .base import ToolHandler, ToolInvocation, ToolOutput


def _coerce_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Coerce argument types based on JSON schema (LLMs sometimes pass strings)."""
    properties = schema.get("properties", {})
    coerced = dict(arguments)

    for key, value in arguments.items():
        if key not in properties or value is None:
            continue

        expected_type = properties[key].get("type")

        if expected_type == "boolean" and not isinstance(value, bool):
            if isinstance(value, str):
                coerced[key] = value.lower() in ("true", "1", "yes")
            else:
                coerced[key] = bool(value)

        elif expected_type == "integer" and not isinstance(value, int):
            try:
                coerced[key] = int(value)
            except (ValueError, TypeError):
                pass  # Keep original, let handler deal with it

        elif expected_type == "number" and not isinstance(value, (int, float)):
            try:
                coerced[key] = float(value)
            except (ValueError, TypeError):
                pass

    return coerced


class ToolRegistry:
    """Registry that stores tool handlers and dispatches invocations."""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self._handlers[handler.name] = handler

    def unregister(self, name: str) -> None:
        """Unregister a tool handler by name (no-op if missing)."""
        self._handlers.pop(name, None)

    def clear(self) -> None:
        """Remove all registered handlers."""
        self._handlers.clear()

    def get(self, name: str) -> ToolHandler | None:
        """Get a handler by name."""
        return self._handlers.get(name)

    def get_specs(self) -> list[dict[str, Any]]:
        """Get all tool specs for the LLM.

        Tools are returned in sorted name order for prompt-cache stability —
        any change in ordering invalidates the cached prefix.
        """
        return [
            self._handlers[name].to_spec()
            for name in sorted(self._handlers)
            if self._handlers[name].is_enabled
        ]

    def requires_approval(self, tool_name: str) -> bool:
        """Check if a tool requires user approval before execution."""
        handler = self._handlers.get(tool_name)
        if handler is None or not handler.is_enabled:
            return True
        return handler.requires_approval

    async def dispatch(self, invocation: ToolInvocation) -> ToolOutput:
        """Dispatch a tool invocation to the appropriate handler."""
        handler = self._handlers.get(invocation.tool_name)
        if handler is None:
            return ToolOutput(
                content=f"Unknown tool: {invocation.tool_name}",
                success=False,
            )

        if not handler.is_enabled:
            return ToolOutput(
                content=f"Tool '{invocation.tool_name}' is currently disabled",
                success=False,
            )

        # Coerce argument types based on schema (LLMs sometimes pass strings)
        coerced_args = _coerce_arguments(invocation.arguments, handler.parameters)
        coerced_invocation = ToolInvocation(
            call_id=invocation.call_id,
            tool_name=invocation.tool_name,
            arguments=coerced_args,
        )

        try:
            return await handler.handle(coerced_invocation)
        except asyncio.CancelledError:
            # Let cancellation propagate up - don't swallow it
            raise
        except Exception as e:
            # Return error to agent so it can self-correct, don't crash CLI
            return ToolOutput(
                content=f"Tool '{invocation.tool_name}' failed: {type(e).__name__}: {e}\nArguments: {invocation.arguments}",
                success=False,
            )

    def __len__(self) -> int:
        return len(self._handlers)

    def __contains__(self, name: str) -> bool:
        return name in self._handlers
