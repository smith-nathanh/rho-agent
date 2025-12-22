"""Core agent loop for ro-agent."""

from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass
from typing import Any

from ..client.model import ModelClient, Prompt, Message
from ..tools.base import ToolInvocation
from ..tools.registry import ToolRegistry
from .session import Session, ToolResult


# Type for approval callback: (tool_name, tool_args) -> approved
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass
class AgentEvent:
    """Event emitted by the agent during execution."""

    type: str  # "text", "tool_start", "tool_end", "turn_complete", "error", "tool_blocked"
    content: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    usage: dict[str, int] | None = None


class Agent:
    """The core agent that orchestrates the conversation loop.

    Follows the pattern:
    1. Build prompt with history and tools
    2. Stream response from model
    3. Execute any tool calls
    4. If tools were called, loop back with results
    5. When model produces final text, turn is complete
    """

    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        client: ModelClient | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self._session = session
        self._registry = registry
        self._client = client or ModelClient()
        self._approval_callback = approval_callback

    async def run_turn(self, user_input: str) -> AsyncIterator[AgentEvent]:
        """Run a single conversation turn.

        This may involve multiple model calls if tools are invoked.
        """
        # Add user message to history
        self._session.add_user_message(user_input)

        # Loop until we get a final response (no more tool calls)
        while True:
            # Build prompt
            prompt = Prompt(
                system=self._session.system_prompt,
                messages=[
                    Message(
                        role=m["role"],
                        content=m.get("content"),
                        tool_calls=m.get("tool_calls"),
                        tool_call_id=m.get("tool_call_id"),
                    )
                    for m in self._session.get_messages()
                ],
                tools=self._registry.get_specs(),
            )

            # Track what we get in this turn
            text_content = ""
            tool_calls: list[dict[str, Any]] = []
            pending_tool_calls: list[tuple[str, str, dict[str, Any]]] = []  # (id, name, args)

            # Stream response
            async for event in self._client.stream(prompt):
                if event.type == "text":
                    text_content += event.content or ""
                    yield AgentEvent(type="text", content=event.content)

                elif event.type == "tool_call":
                    tc = event.tool_call
                    if tc:
                        yield AgentEvent(
                            type="tool_start",
                            tool_name=tc.name,
                            tool_args=tc.arguments,
                        )
                        # OpenAI format for tool calls
                        tool_calls.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": __import__("json").dumps(tc.arguments),
                            },
                        })
                        pending_tool_calls.append((tc.id, tc.name, tc.arguments))

                elif event.type == "done":
                    if event.usage:
                        self._session.update_token_usage(
                            event.usage.get("input_tokens", 0),
                            event.usage.get("output_tokens", 0),
                        )

                elif event.type == "error":
                    yield AgentEvent(type="error", content=event.content)
                    return

            # Record what the assistant said/did
            if tool_calls:
                self._session.add_assistant_tool_calls(tool_calls)
            elif text_content:
                self._session.add_assistant_message(text_content)

            # If no tool calls, we're done
            if not pending_tool_calls:
                yield AgentEvent(
                    type="turn_complete",
                    usage={
                        "total_input_tokens": self._session.total_input_tokens,
                        "total_output_tokens": self._session.total_output_tokens,
                    },
                )
                return

            # Execute tool calls
            tool_results: list[ToolResult] = []
            for tool_id, tool_name, tool_args in pending_tool_calls:
                # Check approval if callback is set
                if self._approval_callback:
                    approved = await self._approval_callback(tool_name, tool_args)
                    if not approved:
                        tool_results.append(ToolResult(
                            tool_call_id=tool_id,
                            content="Command rejected by user",
                        ))
                        yield AgentEvent(
                            type="tool_blocked",
                            tool_name=tool_name,
                            tool_args=tool_args,
                        )
                        continue

                invocation = ToolInvocation(
                    call_id=tool_id,
                    tool_name=tool_name,
                    arguments=tool_args,
                )
                output = await self._registry.dispatch(invocation)
                tool_results.append(ToolResult(
                    tool_call_id=tool_id,
                    content=output.content,
                ))
                yield AgentEvent(
                    type="tool_end",
                    tool_name=tool_name,
                    tool_result=output.content,
                )

            # Add tool results to history and loop
            self._session.add_tool_results(tool_results)
