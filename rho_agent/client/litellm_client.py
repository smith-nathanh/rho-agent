"""LiteLLM client for multi-provider model support with cost tracking.

This client wraps LiteLLM to provide the same interface as ModelClient
while adding support for:
- Multiple providers (OpenAI, Anthropic, Google, etc.)
- Automatic cost tracking via LiteLLM's pricing database
- Provider-specific parameter handling
- Chunk timeout for long-running requests (reasoning_effort)

Usage:
    client = LiteLLMClient(model="anthropic/claude-3-5-sonnet-20241022")
    async for event in client.stream(prompt):
        ...
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from .model import Prompt, StreamEvent, ToolCall


class LiteLLMClient:
    """Client for API calls via LiteLLM.

    Provides the same interface as ModelClient but uses LiteLLM
    for multi-provider support and cost tracking.

    Uses streaming with chunk timeout to handle long-running requests
    (e.g., reasoning_effort=high) while still detecting stuck APIs.
    """

    # Default chunk timeout: how long to wait for next chunk before aborting
    DEFAULT_CHUNK_TIMEOUT: float = 180.0  # 3 minutes between chunks

    def __init__(
        self,
        model: str = "gpt-5-nano",
        api_key: str | None = None,
        chunk_timeout: float | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> None:
        """Initialize the LiteLLM client.

        Args:
            model: Model identifier (e.g., "openai/gpt-5-mini", "anthropic/claude-3-5-sonnet").
            api_key: Optional API key (falls back to environment variables).
            chunk_timeout: Max seconds to wait for next chunk (default: 180).
            temperature: Sampling temperature.
            reasoning_effort: Reasoning effort level for compatible models.
        """
        try:
            import litellm
            self._litellm = litellm
        except ImportError:
            raise ImportError(
                "litellm is required for LiteLLMClient. "
                "Install with: uv pip install rho-agent[evals]"
            )

        self._model = model
        self._api_key = api_key
        self._chunk_timeout = chunk_timeout or self.DEFAULT_CHUNK_TIMEOUT
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort

        # Disable LiteLLM's internal logging to reduce noise
        litellm.suppress_debug_info = True

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

    def _build_kwargs(
        self,
        prompt: Prompt | None = None,
        messages: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build kwargs for litellm.acompletion()."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._build_messages(prompt) if prompt else messages,
            "stream": stream,
        }

        if stream:
            # Request usage in final chunk (OpenAI API feature)
            kwargs["stream_options"] = {"include_usage": True}

        if self._api_key:
            kwargs["api_key"] = self._api_key

        # reasoning_effort and temperature are mutually exclusive
        # reasoning_effort is for OpenAI o-series/GPT-5 models
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            kwargs["temperature"] = self._temperature

        if prompt and prompt.tools:
            kwargs["tools"] = prompt.tools

        return kwargs

    def _extract_usage(self, usage_obj: Any) -> dict[str, Any]:
        """Extract usage dict from usage object including cost."""
        usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
        }

        if usage_obj:
            usage["input_tokens"] = getattr(usage_obj, "prompt_tokens", 0) or 0
            usage["output_tokens"] = getattr(usage_obj, "completion_tokens", 0) or 0

            # Extract cached tokens if available (prompt_tokens_details)
            if hasattr(usage_obj, "prompt_tokens_details"):
                details = usage_obj.prompt_tokens_details
                if details and hasattr(details, "cached_tokens"):
                    usage["cached_tokens"] = details.cached_tokens or 0

            # Extract reasoning tokens if available (completion_tokens_details)
            # Reasoning tokens are a subset of completion_tokens, not additive
            if hasattr(usage_obj, "completion_tokens_details"):
                details = usage_obj.completion_tokens_details
                if details and hasattr(details, "reasoning_tokens"):
                    usage["reasoning_tokens"] = details.reasoning_tokens or 0

        # Compute cost using LiteLLM's pricing database
        try:
            cost = self._litellm.completion_cost(
                model=self._model,
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
            )
            if cost:
                usage["cost_usd"] = cost
        except Exception:
            pass

        return usage

    async def stream(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Process a prompt and yield events with streaming.

        Uses chunk timeout to handle long-running requests while
        detecting stuck APIs. If no chunk is received within chunk_timeout
        seconds, the request is aborted.
        """
        kwargs = self._build_kwargs(prompt=prompt, stream=True)

        try:
            response = await self._litellm.acompletion(**kwargs)

            # Accumulate tool calls (they come in pieces via deltas)
            tool_calls_in_progress: dict[int, dict[str, Any]] = {}
            usage: dict[str, Any] = {}

            async for chunk in self._iter_with_timeout(response):
                # Check for usage in chunk (comes in final chunk with stream_options)
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = self._extract_usage(chunk.usage)

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # Content delta
                if delta.content:
                    yield StreamEvent(type="text", content=delta.content)

                # Tool call deltas
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_in_progress:
                            tool_calls_in_progress[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        tc = tool_calls_in_progress[idx]

                        if tc_delta.id:
                            tc["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tc["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tc["arguments"] += tc_delta.function.arguments

            # Emit completed tool calls
            for tc in tool_calls_in_progress.values():
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                yield StreamEvent(
                    type="tool_call",
                    tool_call=ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=args,
                    ),
                )

            # Emit done with usage
            yield StreamEvent(type="done", usage=usage)

        except asyncio.TimeoutError:
            yield StreamEvent(
                type="error",
                content=f"Chunk timeout: no response received for {self._chunk_timeout}s",
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e))

    async def _iter_with_timeout(self, response: Any) -> AsyncIterator[Any]:
        """Iterate over streaming response with chunk timeout.

        Raises asyncio.TimeoutError if no chunk is received within
        chunk_timeout seconds.
        """
        aiter = response.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(),
                    timeout=self._chunk_timeout,
                )
                yield chunk
            except StopAsyncIteration:
                break

    async def complete(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        """Non-streaming completion for simple requests.

        Returns (content, usage_dict) where usage_dict may include cost_usd.
        """
        try:
            kwargs = self._build_kwargs(messages=messages, stream=False)
            # Add a generous timeout for non-streaming calls
            kwargs["timeout"] = 600.0  # 10 minutes
            response = await self._litellm.acompletion(**kwargs)

            content = response.choices[0].message.content or ""
            usage = self._extract_usage(response.usage)

            return content, usage

        except Exception as e:
            return f"Error: {e}", {"input_tokens": 0, "output_tokens": 0}
