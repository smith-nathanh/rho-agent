"""Base classes for the tool system."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolOutput:
    """Result of a tool execution."""

    content: str
    success: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolInvocation:
    """A request to invoke a tool."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


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
    def requires_approval(self) -> bool:
        """Whether this tool requires user approval before execution.

        Override to return True for potentially dangerous tools.
        Safe read-only tools should leave this as False.
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
