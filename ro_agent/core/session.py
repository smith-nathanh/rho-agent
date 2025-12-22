"""Session management for conversation history."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Result of a tool call to include in history."""

    tool_call_id: str
    content: str


@dataclass
class Session:
    """Manages conversation state and history.

    History is stored in OpenAI's message format.
    """

    system_prompt: str
    history: list[dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def add_user_message(self, content: str) -> None:
        """Add a user message to history."""
        self.history.append({
            "role": "user",
            "content": content,
        })

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message to history."""
        self.history.append({
            "role": "assistant",
            "content": content,
        })

    def add_assistant_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Add assistant message with tool calls."""
        self.history.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        })

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Add tool results as tool messages."""
        for r in results:
            self.history.append({
                "role": "tool",
                "tool_call_id": r.tool_call_id,
                "content": r.content,
            })

    def update_token_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Update cumulative token usage."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    def get_messages(self) -> list[dict[str, Any]]:
        """Get history in API format."""
        return self.history.copy()

    def clear(self) -> None:
        """Clear conversation history."""
        self.history.clear()

    def compact(self) -> None:
        """Placeholder for future compaction implementation.

        Would summarize older messages to reduce context size.
        """
        pass
