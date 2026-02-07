"""Cerebras client for eval using native SDK.

This client is used only for evals when OPENAI_BASE_URL contains 'cerebras'.
It uses the native Cerebras SDK which properly supports parallel_tool_calls=False.
"""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from cerebras.cloud.sdk import AsyncCerebras


@dataclass
class ToolCall:
    """A tool call from the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StreamEvent:
    """An event from the model stream."""

    type: str  # "text", "tool_call", "done", "error"
    content: str | None = None
    tool_call: ToolCall | None = None
    stop_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class Message:
    """A message in the conversation."""

    role: str  # "user", "assistant", "system", "tool"
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


@dataclass
class Prompt:
    """A prompt to send to the model."""

    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)


def _make_strict_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transform tool specs to use Cerebras strict mode.

    Adds strict: true and additionalProperties: false for guaranteed
    schema compliance with constrained decoding.
    """
    strict_tools = []
    for tool in tools:
        tool = tool.copy()
        if "function" in tool:
            func = tool["function"] = tool["function"].copy()
            func["strict"] = True
            if "parameters" in func:
                params = func["parameters"] = func["parameters"].copy()
                params["additionalProperties"] = False
        strict_tools.append(tool)
    return strict_tools


class CerebrasClient:
    """Client for Cerebras API using native SDK.

    Used for evals only - supports parallel_tool_calls=False properly.
    """

    def __init__(
        self,
        model: str = "llama-3.3-70b",
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        # Use CEREBRAS_API_KEY or fall back to OPENAI_API_KEY
        if api_key is None:
            api_key = os.environ.get("CEREBRAS_API_KEY") or os.environ.get("OPENAI_API_KEY")

        self._client = AsyncCerebras(api_key=api_key, timeout=timeout)
        self._model = model

    def _build_messages(self, prompt: Prompt) -> list[dict[str, Any]]:
        """Build messages list from prompt."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt.system}]
        for msg in prompt.messages:
            m: dict[str, Any] = {"role": msg.role}
            if msg.content is not None:
                m["content"] = msg.content
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            messages.append(m)
        return messages

    async def stream(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Non-streaming completion that yields StreamEvents.

        Cerebras doesn't support streaming with tools, so this uses
        non-streaming calls but yields events in the same format.
        """
        messages = self._build_messages(prompt)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }

        if prompt.tools:
            kwargs["tools"] = _make_strict_tools(prompt.tools)
            kwargs["parallel_tool_calls"] = False  # Force sequential
            # Debug: confirm we're using strict tools
            import sys

            print(
                f"[CerebrasClient] Using {len(prompt.tools)} strict tools, parallel_tool_calls=False",
                file=sys.stderr,
            )

        try:
            response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message

            # Emit text content if present
            if msg.content:
                yield StreamEvent(type="text", content=msg.content)

            # Emit tool calls if present
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield StreamEvent(
                        type="tool_call",
                        tool_call=ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=args,
                        ),
                    )

            # Emit done with usage
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
            yield StreamEvent(type="done", usage=usage)

        except Exception as e:
            yield StreamEvent(type="error", content=str(e))

    async def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
        """Non-streaming completion for simple requests like summarization."""
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
            )
            content = response.choices[0].message.content or ""
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
            return content, usage
        except Exception as e:
            return f"Error: {e}", {"input_tokens": 0, "output_tokens": 0}
