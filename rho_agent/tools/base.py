"""Base classes for the tool system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolOutput:
    """Result of a tool execution."""

    content: str
    success: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    # Images for the model to look at: dicts with ``path``, ``media_type`` and
    # base64 ``data``. The session attaches them after the turn's tool results.
    images: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ToolInvocation:
    """A request to invoke a tool."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # The Session making the call, when there is one. Tools that need session
    # context (such as delegate) read it; most tools ignore it.
    session: Any | None = field(default=None, repr=False, compare=False)


class ToolHandler(ABC):
    """Abstract base class for tool handlers.

    Each handler implements a specific tool capability (shell execution,
    database queries, file reading, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this tool."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for the tool's parameters."""
        ...

    @property
    def is_enabled(self) -> bool:
        """Whether this tool is currently available to the LLM.

        Override to implement dynamic enablement based on handler state.
        Default: always enabled.
        """
        return True

    @property
    def requires_approval(self) -> bool:
        """Whether this tool requires user approval before execution.

        Override to return True for potentially dangerous tools.
        Safe read-only tools should leave this as False.
        """
        return False

    @property
    def parallel_safe(self) -> bool:
        """Whether several calls to this tool may run concurrently in one turn.

        True only for tools with no side effects on shared state (reads,
        searches). The session runs a model turn's calls together only when
        every call in it is parallel-safe and none needs approval.
        """
        return False

    @abstractmethod
    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the tool and return the result."""
        ...

    def to_spec(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
