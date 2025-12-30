"""Tool registry for storing and dispatching to handlers."""

from typing import Any

from .base import ToolHandler, ToolInvocation, ToolOutput


class ToolRegistry:
    """Registry that stores tool handlers and dispatches invocations."""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self._handlers[handler.name] = handler

    def get(self, name: str) -> ToolHandler | None:
        """Get a handler by name."""
        return self._handlers.get(name)

    def get_specs(self) -> list[dict[str, Any]]:
        """Get all tool specs for the LLM."""
        return [handler.to_spec() for handler in self._handlers.values()]

    def requires_approval(self, tool_name: str) -> bool:
        """Check if a tool requires user approval before execution."""
        handler = self._handlers.get(tool_name)
        return handler.requires_approval if handler else True

    async def dispatch(self, invocation: ToolInvocation) -> ToolOutput:
        """Dispatch a tool invocation to the appropriate handler."""
        handler = self._handlers.get(invocation.tool_name)
        if handler is None:
            return ToolOutput(
                content=f"Unknown tool: {invocation.tool_name}",
                success=False,
            )
        return await handler.handle(invocation)

    def __len__(self) -> int:
        return len(self._handlers)

    def __contains__(self, name: str) -> bool:
        return name in self._handlers
