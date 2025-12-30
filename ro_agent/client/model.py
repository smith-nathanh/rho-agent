"""Model client for streaming API calls via OpenAI-compatible API."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI


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


class ModelClient:
    """Client for streaming API calls via OpenAI-compatible API.

    Works with OpenAI, vLLM, or any OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        model: str = "gpt-5-nano",
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    async def stream(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Stream a response from the model."""
        # Build messages
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

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if prompt.tools:
            kwargs["tools"] = prompt.tools

        try:
            # Track tool calls being built
            tool_calls_in_progress: dict[int, dict[str, str]] = {}

            async with await self._client.chat.completions.create(**kwargs) as stream:
                async for chunk in stream:
                    if not chunk.choices:
                        # Usage info comes in final chunk with no choices
                        if chunk.usage:
                            yield StreamEvent(
                                type="done",
                                usage={
                                    "input_tokens": chunk.usage.prompt_tokens,
                                    "output_tokens": chunk.usage.completion_tokens,
                                },
                            )
                        continue

                    choice = chunk.choices[0]
                    delta = choice.delta

                    # Text content
                    if delta.content:
                        yield StreamEvent(type="text", content=delta.content)

                    # Tool calls
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_in_progress:
                                tool_calls_in_progress[idx] = {
                                    "id": tc.id or "",
                                    "name": tc.function.name if tc.function else "",
                                    "arguments": "",
                                }
                            if tc.id:
                                tool_calls_in_progress[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_in_progress[idx]["name"] = (
                                        tc.function.name
                                    )
                                if tc.function.arguments:
                                    tool_calls_in_progress[idx]["arguments"] += (
                                        tc.function.arguments
                                    )

                    # Check for finish
                    if choice.finish_reason:
                        # Emit any completed tool calls
                        for tc_data in tool_calls_in_progress.values():
                            try:
                                args = (
                                    json.loads(tc_data["arguments"])
                                    if tc_data["arguments"]
                                    else {}
                                )
                            except json.JSONDecodeError:
                                args = {}
                            yield StreamEvent(
                                type="tool_call",
                                tool_call=ToolCall(
                                    id=tc_data["id"],
                                    name=tc_data["name"],
                                    arguments=args,
                                ),
                            )
                        tool_calls_in_progress.clear()

        except Exception as e:
            yield StreamEvent(type="error", content=str(e))
