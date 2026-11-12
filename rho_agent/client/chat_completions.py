"""Chat Completions client for OpenAI-compatible servers other than OpenAI.

``ModelClient`` talks to OpenAI through the Responses API. Most third-party
servers (vLLM, Ollama, Cerebras, ...) only implement ``/v1/chat/completions``,
so ``ModelClient`` hands its calls to this class when a custom base URL is set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from .model import (
    ModelCallError,
    Prompt,
    StreamEvent,
    ToolCall,
    classify_error,
    parse_tool_arguments,
)


class ChatCompletionsClient:
    """Streaming client for ``/v1/chat/completions``."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        base_url: str = "",
        service_tier: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._service_tier = service_tier
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._response_format = response_format
        # Cerebras doesn't support streaming with tool calling
        self._use_nonstreaming_tools = "cerebras" in base_url.lower()

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
        elif self._temperature is not None:
            kwargs["temperature"] = self._temperature

        if prompt.tools:
            kwargs["tools"] = prompt.tools

        if self._service_tier:
            kwargs["service_tier"] = self._service_tier

        if self._response_format:
            kwargs["response_format"] = self._response_format

        try:
            # Track tool calls being built
            tool_calls_in_progress: dict[int, dict[str, str]] = {}

            async with await self._client.chat.completions.create(**kwargs) as stream:
                async for chunk in stream:
                    if not chunk.choices:
                        # Usage info comes in final chunk with no choices
                        if chunk.usage:
                            cached = 0
                            details = getattr(chunk.usage, "prompt_tokens_details", None)
                            if details:
                                cached = getattr(details, "cached_tokens", 0) or 0
                            yield StreamEvent(
                                type="done",
                                usage={
                                    "input_tokens": chunk.usage.prompt_tokens,
                                    "output_tokens": chunk.usage.completion_tokens,
                                    "cached_tokens": cached,
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
                                    tool_calls_in_progress[idx]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tool_calls_in_progress[idx]["arguments"] += (
                                        tc.function.arguments
                                    )

                    # Check for finish
                    if choice.finish_reason:
                        # Emit any completed tool calls
                        for tc_data in tool_calls_in_progress.values():
                            args, parse_error = parse_tool_arguments(tc_data["arguments"])
                            yield StreamEvent(
                                type="tool_call",
                                tool_call=ToolCall(
                                    id=tc_data["id"],
                                    name=tc_data["name"],
                                    arguments=args,
                                    parse_error=parse_error,
                                ),
                            )
                        tool_calls_in_progress.clear()

        except APIStatusError as e:
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
                error_kind=classify_error(e),
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e), error_kind=classify_error(e))

    async def _stream_via_complete(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Non-streaming fallback for providers that don't stream tool calls."""
        messages = self._build_messages(prompt)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }

        # reasoning_effort and temperature are mutually exclusive
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        elif self._temperature is not None:
            kwargs["temperature"] = self._temperature

        if prompt.tools:
            kwargs["tools"] = prompt.tools

        if self._service_tier:
            kwargs["service_tier"] = self._service_tier

        if self._response_format:
            kwargs["response_format"] = self._response_format

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
                    args, parse_error = parse_tool_arguments(tc.function.arguments or "")
                    yield StreamEvent(
                        type="tool_call",
                        tool_call=ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=args,
                            parse_error=parse_error,
                        ),
                    )

            # Emit done with usage
            cached = 0
            if response.usage:
                details = getattr(response.usage, "prompt_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
                "cached_tokens": cached,
            }
            yield StreamEvent(type="done", usage=usage)

        except APIStatusError as e:
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
                error_kind=classify_error(e),
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e), error_kind=classify_error(e))

    async def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """Non-streaming completion returning (content, usage_dict)."""
        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "stream": False,
            }
            # reasoning_effort and temperature are mutually exclusive
            if self._reasoning_effort:
                kwargs["reasoning_effort"] = self._reasoning_effort
            elif self._temperature is not None:
                kwargs["temperature"] = self._temperature
            if self._service_tier:
                kwargs["service_tier"] = self._service_tier
            if self._response_format:
                kwargs["response_format"] = self._response_format

            response = await self._client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            cached = 0
            if response.usage:
                details = getattr(response.usage, "prompt_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
                "cached_tokens": cached,
                "cost_usd": 0.0,
            }
            return content, usage
        except APIStatusError as e:
            return (
                f"API error {e.status_code} (after retries): {e.message}",
                {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0},
            )
        except Exception as e:
            return f"Error: {e}", {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cost_usd": 0.0,
            }

    async def complete_prompt(self, prompt: Prompt) -> tuple[str, dict[str, Any]]:
        """Non-streaming completion from a Prompt, preserving system/tools prefix.

        Like complete() but accepts a Prompt so the system prompt and tool
        definitions are included in the request — enabling prompt-cache reuse
        when the prefix matches a previous streaming call.

        Raises:
            ModelCallError: if the call fails. The error text is never returned
                as content, because callers use the content as a summary.
        """
        messages = self._build_messages(prompt)
        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "stream": False,
            }
            if self._reasoning_effort:
                kwargs["reasoning_effort"] = self._reasoning_effort
            elif self._temperature is not None:
                kwargs["temperature"] = self._temperature
            if prompt.tools:
                kwargs["tools"] = prompt.tools
                if prompt.tool_choice:
                    kwargs["tool_choice"] = prompt.tool_choice
            if self._service_tier:
                kwargs["service_tier"] = self._service_tier

            response = await self._client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            raise ModelCallError(
                f"API error {e.status_code} (after retries): {e.message}", classify_error(e)
            ) from e
        except Exception as e:
            raise ModelCallError(str(e), classify_error(e)) from e

        content = response.choices[0].message.content or ""
        cached = 0
        if response.usage:
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details:
                cached = getattr(details, "cached_tokens", 0) or 0
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
            "cached_tokens": cached,
            "cost_usd": 0.0,
        }
        return content, usage
