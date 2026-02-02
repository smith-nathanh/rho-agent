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
    total_cached_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_usd: float = 0.0
    last_input_tokens: int = 0  # Context size of most recent API call

    def add_user_message(self, content: str) -> None:
        """Add a user message to history."""
        self.history.append(
            {
                "role": "user",
                "content": content,
            }
        )

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message to history."""
        self.history.append(
            {
                "role": "assistant",
                "content": content,
            }
        )

    def add_assistant_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Add assistant message with tool calls."""
        self.history.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Add tool results as tool messages."""
        for r in results:
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": r.tool_call_id,
                    "content": r.content,
                }
            )

    def update_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Update cumulative token usage and cost.

        Args:
            input_tokens: Number of input tokens consumed.
            output_tokens: Number of output tokens generated.
            cached_tokens: Number of cached tokens (provider-dependent).
            reasoning_tokens: Number of reasoning tokens (subset of output_tokens).
            cost_usd: Cost in USD for this API call.
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cached_tokens += cached_tokens
        self.total_reasoning_tokens += reasoning_tokens
        self.total_cost_usd += cost_usd
        if input_tokens > 0:
            self.last_input_tokens = input_tokens

    def get_messages(self) -> list[dict[str, Any]]:
        """Get history in API format."""
        return self.history.copy()

    def clear(self) -> None:
        """Clear conversation history."""
        self.history.clear()

    def replace_with_summary(
        self, summary: str, recent_user_messages: list[str] | None = None
    ) -> None:
        """Replace history with a compacted summary.

        Args:
            summary: The summary text from compaction.
            recent_user_messages: Optional list of recent user messages to preserve.
        """
        self.history.clear()

        # Add recent user messages if provided
        if recent_user_messages:
            for msg in recent_user_messages:
                self.history.append({"role": "user", "content": msg})

        # Add the summary as a user message (following Codex pattern)
        self.history.append({"role": "user", "content": summary})

    def get_user_messages(self) -> list[str]:
        """Extract all user messages from history."""
        return [
            m["content"]
            for m in self.history
            if m.get("role") == "user" and m.get("content")
        ]

    def estimate_tokens(self) -> int:
        """Rough estimate of tokens in history (4 chars ≈ 1 token)."""
        total_chars = len(self.system_prompt)
        for m in self.history:
            content = m.get("content")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                total_chars += sum(len(str(c)) for c in content)
            # Tool calls
            if m.get("tool_calls"):
                total_chars += len(str(m["tool_calls"]))
        return total_chars // 4
