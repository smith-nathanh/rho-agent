"""Model client for streaming API calls via OpenAI-compatible API."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, APIStatusError


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
        timeout: float | None = None,
        service_tier: str | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> None:
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        # For flex processing, use longer timeout (15 min) per OpenAI docs
        if timeout is None:
            timeout = 900.0 if service_tier == "flex" else 60.0
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=8,
        )
        self._model = model
        self._service_tier = service_tier
        self._base_url = base_url or ""

        # Cerebras doesn't support streaming with tool calling
        self._use_nonstreaming_tools = "cerebras" in self._base_url.lower()

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
        """Stream a response from the model."""
        # Use non-streaming for tool calls on providers that don't support it
        if prompt.tools and self._use_nonstreaming_tools:
            async for event in self._stream_via_complete(prompt):
                yield event
            return

        messages = self._build_messages(prompt)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # reasoning_effort and temperature are mutually exclusive
        # GPT-5/o-series models don't support temperature with reasoning
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            kwargs["temperature"] = self._temperature

        if prompt.tools:
            kwargs["tools"] = prompt.tools

        if self._service_tier:
            kwargs["service_tier"] = self._service_tier

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

        except APIStatusError as e:
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e))

    async def _stream_via_complete(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Non-streaming tool calling that yields StreamEvents.

        Used for providers like Cerebras that don't support streaming with tools.
        """
        messages = self._build_messages(prompt)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }

        # reasoning_effort and temperature are mutually exclusive
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            kwargs["temperature"] = self._temperature

        if prompt.tools:
            kwargs["tools"] = prompt.tools

        if self._service_tier:
            kwargs["service_tier"] = self._service_tier

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

        except APIStatusError as e:
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e))

    async def complete(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int]]:
        """Non-streaming completion for simple requests like summarization.

        Returns (content, usage_dict).
        """
        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "stream": False,
            }
            # reasoning_effort and temperature are mutually exclusive
            if self._reasoning_effort:
                kwargs["reasoning_effort"] = self._reasoning_effort
            else:
                kwargs["temperature"] = self._temperature
            if self._service_tier:
                kwargs["service_tier"] = self._service_tier

            response = await self._client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens
                if response.usage
                else 0,
            }
            return content, usage
        except APIStatusError as e:
            return (
                f"API error {e.status_code} (after retries): {e.message}",
                {"input_tokens": 0, "output_tokens": 0},
            )
        except Exception as e:
            return f"Error: {e}", {"input_tokens": 0, "output_tokens": 0}
