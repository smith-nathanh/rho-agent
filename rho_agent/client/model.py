"""Model client for streaming API calls via OpenAI-compatible API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, AsyncOpenAI

CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "too many tokens",
    "input is too long",
)

# HTTP statuses where retrying the same request cannot help.
_FATAL_STATUSES = frozenset({400, 401, 403, 404, 422})


def classify_error(exc: BaseException) -> str:
    """Classify a provider error as ``context_overflow``, ``fatal`` or ``transient``."""
    text = str(exc).lower()
    if any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS):
        return "context_overflow"
    status = getattr(exc, "status_code", None)
    if status in _FATAL_STATUSES:
        return "fatal"
    return "transient"


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """Parse tool-call arguments, returning ``(arguments, parse_error)``.

    On failure the arguments are empty and ``parse_error`` describes the
    problem, so the session can tell the model instead of running the tool
    with no arguments.
    """
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        preview = raw if len(raw) <= 500 else raw[:500] + "..."
        return {}, f"arguments were not valid JSON ({e.msg} at char {e.pos}): {preview}"
    if not isinstance(parsed, dict):
        return {}, f"arguments must be a JSON object, got {type(parsed).__name__}"
    return parsed, None


class ModelCallError(Exception):
    """A non-streaming model call failed. ``kind`` is as in ``classify_error``."""

    def __init__(self, message: str, kind: str = "transient") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ToolCall:
    """A tool call from the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model's arguments could not be parsed; the tool must not run.
    parse_error: str | None = None


@dataclass
class StreamEvent:
    """An event from the model stream.

    Usage dict (for type="done") may contain:
        - input_tokens: int
        - output_tokens: int
        - cached_tokens: int (optional, provider-dependent)
        - cost_usd: float (optional, from LiteLLMClient)
    """

    type: str  # "text", "tool_call", "done", "error"
    content: str | None = None
    tool_call: ToolCall | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    # For type="error": "context_overflow", "fatal" or "transient".
    error_kind: str | None = None


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
    # "none" keeps the tool definitions in the request (so a cached prefix is
    # still reused) while forbidding the model from calling one.
    tool_choice: str | None = None


class ModelClient:
    """Streaming model client for OpenAI-compatible APIs."""

    def __init__(
        self,
        model: str = "gpt-6.1-terra",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        service_tier: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> None:
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._response_format = response_format
        # For flex processing, use longer timeout (15 min) per OpenAI docs
        if timeout is None:
            timeout = 900.0 if service_tier == "flex" else 60.0
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=8,
        )
        self._model = model
        self._service_tier = service_tier
        self._base_url = base_url or ""

        # Cerebras doesn't support streaming with tool calling
        self._use_nonstreaming_tools = "cerebras" in self._base_url.lower()
        # Set once the endpoint has refused tools together with reasoning.
        self._tools_need_reasoning_off = False

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        """Call chat.completions.create, turning reasoning off for tools if required.

        Some models (the GPT-5.6 and GPT-6 families) reject function tools on
        this endpoint unless ``reasoning_effort`` is ``"none"``. When no effort
        was configured, the request is retried once with reasoning off and
        later tool requests are sent that way. An explicitly configured effort
        is never overridden: that error is raised to the caller.
        """
        effort_unset = "reasoning_effort" not in kwargs
        if self._tools_need_reasoning_off and kwargs.get("tools") and effort_unset:
            kwargs["reasoning_effort"] = "none"
        try:
            return await self._client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            message = str(e.message).lower()
            refused = e.status_code == 400 and "reasoning_effort" in message and "tools" in message
            if not (refused and kwargs.get("tools") and effort_unset):
                raise
            self._tools_need_reasoning_off = True
            kwargs["reasoning_effort"] = "none"
            return await self._client.chat.completions.create(**kwargs)

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

            async with await self._create(kwargs) as stream:
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
            response = await self._create(kwargs)
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

            response = await self._create(kwargs)
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

            response = await self._create(kwargs)
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
