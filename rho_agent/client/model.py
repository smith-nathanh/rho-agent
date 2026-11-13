"""Model client built on the Responses API.

Conversation history is kept in chat format (``Prompt.messages``) and converted
to Responses input items for each request. Requests are stateless
(``store=False``): the reasoning items a model returns are stored, encrypted,
on the assistant message and sent back on the next request, so the trace file
alone is enough to continue a session.

Servers without a Responses endpoint are served by ``ChatCompletionsClient``;
see ``ModelClient`` for how the endpoint is chosen.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from openai import APIStatusError, AsyncOpenAI

from .pricing import Prices, call_cost, can_price, prices_from_env

# The only environment variables rho-agent reads for the model connection.
# OPENAI_API_KEY and OPENAI_BASE_URL are deliberately ignored, so a key set
# for another tool is never sent anywhere by accident.
API_KEY_ENV = "RHO_AGENT_API_KEY"
BASE_URL_ENV = "RHO_AGENT_BASE_URL"
MODEL_ENV = "RHO_AGENT_MODEL"
DEFAULT_MODEL = "gpt-6.1-terra"
OPENAI_ENDPOINT = "https://api.openai.com/v1"


def default_model() -> str:
    """``RHO_AGENT_MODEL``, or the built-in default."""
    return os.getenv(MODEL_ENV) or DEFAULT_MODEL


class MissingApiKeyError(RuntimeError):
    """No API key was given for a server that needs one."""

    def __init__(self) -> None:
        super().__init__(
            f"Missing API key. Set {API_KEY_ENV} to your provider's key "
            f"(and {BASE_URL_ENV} to its endpoint if it is not OpenAI)."
        )


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
        - reasoning_tokens: int (optional, already counted in output_tokens)
        - cost_usd: float (0.0 when no price is known; see ``pricing``)
    """

    type: str  # "text", "tool_call", "done", "error"
    content: str | None = None
    tool_call: ToolCall | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    # For type="error": "context_overflow", "fatal" or "transient".
    error_kind: str | None = None
    # For type="done": the response's output items in order, to be stored on
    # the assistant message (see ``Message.response_items``). None when the
    # response had no reasoning, in which case the chat fields are enough.
    response_items: list[dict[str, Any]] | None = None


@dataclass
class Message:
    """A message in the conversation."""

    role: str  # "user", "assistant", "system", "tool"
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    # Assistant messages only: the Responses output items this message was
    # built from, in the order the model produced them. Reasoning items are
    # stored whole (with their encrypted content), message items as
    # ``{"type": "message", "role": "assistant", "content": text}`` and function
    # calls as ``{"type": "function_call", "call_id": id}``, which refers to the
    # entry in ``tool_calls``.
    response_items: list[dict[str, Any]] | None = None


@dataclass
class Prompt:
    """A prompt to send to the model."""

    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    # "none" keeps the tool definitions in the request (so a cached prefix is
    # still reused) while forbidding the model from calling one.
    tool_choice: str | None = None
    # The rho-agent session making the call. Sent to OpenRouter only, which
    # groups calls with the same session ID into one trace.
    session_id: str | None = None


# --- Chat format -> Responses format ---------------------------------------


def _content_parts(content: list[Any]) -> list[dict[str, Any]]:
    """Convert chat content parts (text, image_url) to Responses input parts."""
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"type": "input_text", "text": str(part)})
        elif part.get("type") == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            if isinstance(image, dict):
                url, detail = image.get("url"), image.get("detail")
            else:
                url, detail = image, None
            parts.append({"type": "input_image", "image_url": url, "detail": detail or "auto"})
        else:
            parts.append(part)  # already in Responses format
    return parts


def _text_of(content: Any) -> str:
    """Plain text of chat content that may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict) and "text" in p
        )
    return "" if content is None else str(content)


def _function_call_item(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    return {
        "type": "function_call",
        "call_id": tool_call.get("id") or "",
        "name": function.get("name") or "",
        "arguments": function.get("arguments") or "{}",
    }


def _assistant_items(msg: Message) -> list[dict[str, Any]]:
    """Input items for one assistant message.

    With ``response_items`` the model's own item order is replayed, reasoning
    included. Function calls are always taken from ``tool_calls``, so a call
    that is no longer in the history is not sent, and one that is not referenced
    is still sent.
    """
    calls = {tc.get("id"): tc for tc in msg.tool_calls or []}
    items: list[dict[str, Any]] = []
    if msg.response_items:
        for item in msg.response_items:
            if item.get("type") == "function_call":
                tool_call = calls.pop(item.get("call_id"), None)
                if tool_call is not None:
                    items.append(_function_call_item(tool_call))
            else:
                items.append(item)
    else:
        text = _text_of(msg.content)
        if text:
            items.append({"role": "assistant", "content": text})
    items.extend(_function_call_item(tc) for tc in calls.values())
    return items


def to_response_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert chat-format history into Responses input items."""
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            items.extend(_assistant_items(msg))
        elif msg.role == "tool":
            output: Any = msg.content
            if isinstance(output, list):
                output = _content_parts(output)
            elif output is None:
                output = ""
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": output,
                }
            )
        else:
            content: Any = msg.content
            if isinstance(content, list):
                content = _content_parts(content)
            elif content is None:
                content = ""
            items.append({"role": msg.role, "content": content})
    return items


def to_response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert chat-format function tools to Responses function tools.

    ``strict`` is sent as false unless the tool asks for it: the Responses API
    otherwise treats it as true and rejects schemas with optional parameters.
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            converted.append(tool)  # already in Responses format
            continue
        converted.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                "strict": bool(function.get("strict", False)),
            }
        )
    return converted


def to_response_tool_choice(tool_choice: Any) -> Any:
    """Convert a chat-format ``tool_choice`` to the Responses form."""
    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
        return {"type": "function", "name": tool_choice["function"].get("name")}
    return tool_choice


def to_response_text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    """Convert a chat-format ``response_format`` to a Responses ``text.format``."""
    schema = response_format.get("json_schema")
    if response_format.get("type") == "json_schema" and isinstance(schema, dict):
        return {"type": "json_schema", **schema}
    return response_format


# --- Responses output -> events ---------------------------------------------


def _message_text(item: Any) -> str:
    """Text of an output message item, refusals included."""
    text = ""
    for part in getattr(item, "content", None) or []:
        text += getattr(part, "text", None) or getattr(part, "refusal", None) or ""
    return text


def _reasoning_item(item: Any) -> dict[str, Any] | None:
    """Input form of a reasoning output item, or None if it cannot be replayed.

    Without ``encrypted_content`` the item only exists on OpenAI's side, and a
    stateless request that refers to it is rejected.
    """
    encrypted = getattr(item, "encrypted_content", None)
    if not encrypted:
        return None
    return {
        "type": "reasoning",
        "id": item.id,
        "summary": [
            {"type": "summary_text", "text": s.text} for s in getattr(item, "summary", None) or []
        ],
        "encrypted_content": encrypted,
    }


def _record_item(item: Any, items: list[dict[str, Any]]) -> ToolCall | None:
    """Add a finished output item to ``items``; return its tool call if it is one."""
    kind = getattr(item, "type", None)
    if kind == "reasoning":
        reasoning = _reasoning_item(item)
        if reasoning is not None:
            items.append(reasoning)
    elif kind == "message":
        message: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "content": _message_text(item),
        }
        if getattr(item, "phase", None):
            message["phase"] = item.phase
        if message["content"]:
            items.append(message)
    elif kind == "function_call":
        items.append({"type": "function_call", "call_id": item.call_id})
        args, parse_error = parse_tool_arguments(item.arguments or "")
        return ToolCall(id=item.call_id, name=item.name, arguments=args, parse_error=parse_error)
    return None


def _items_to_store(items: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The items worth storing: only when they hold more than the chat fields do."""
    if any(i.get("type") == "reasoning" or i.get("phase") for i in items):
        return items
    return None


def _field(obj: Any, name: str) -> Any:
    """``obj.name`` or ``obj[name]``: extra fields the SDK keeps may be plain dicts."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def reported_cost(usage: Any) -> float | None:
    """Cost in USD the server reported for a call, or None (OpenAI reports none).

    OpenRouter puts the amount charged in ``usage.cost``. With your own provider
    key (``is_byok``), that is only OpenRouter's fee; the provider's charge is
    in ``cost_details.upstream_inference_cost`` and is added.
    """
    cost = _field(usage, "cost")
    if not isinstance(cost, int | float) or isinstance(cost, bool):
        return None
    total = float(cost)
    if _field(usage, "is_byok") is True:
        upstream = _field(_field(usage, "cost_details"), "upstream_inference_cost")
        if isinstance(upstream, int | float):
            total += float(upstream)
    return total


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    result: dict[str, Any] = {
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cached_tokens": getattr(input_details, "cached_tokens", 0) or 0,
        # Already counted in output_tokens.
        "reasoning_tokens": getattr(output_details, "reasoning_tokens", 0) or 0,
    }
    cost = reported_cost(usage)
    if cost is not None:
        result["cost_usd"] = cost
    return result


def _stop_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    return getattr(details, "reason", None) or getattr(response, "status", None)


_TRANSIENT_RESPONSE_ERRORS = frozenset(
    {"server_error", "rate_limit_exceeded", "vector_store_timeout"}
)


def _response_error(code: str | None, message: str | None) -> StreamEvent:
    """Error event for a failure reported inside a response, not as an HTTP status."""
    text = f"{code}: {message}" if code else (message or "response failed")
    if any(marker in text.lower() for marker in CONTEXT_OVERFLOW_MARKERS):
        kind = "context_overflow"
    elif code is None or code in _TRANSIENT_RESPONSE_ERRORS:
        kind = "transient"
    else:
        kind = "fatal"
    return StreamEvent(type="error", content=text, error_kind=kind)


_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}


# Statuses a server without a Responses endpoint answers ``/v1/responses`` with.
_NO_ENDPOINT_STATUSES = frozenset({404, 405})


def _requested_api(api: str | None) -> str | None:
    """The endpoint pinned by ``api`` or ``RHO_AGENT_API``, if any."""
    api = api or os.getenv("RHO_AGENT_API")
    if api and api not in ("responses", "chat"):
        raise ValueError(f"api must be 'responses' or 'chat', got {api!r}")
    return api or None


def _is_openrouter_host(base_url: str | None) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _is_openai_host(base_url: str | None) -> bool:
    """Whether requests go to OpenAI itself (no base URL, OpenAI or Azure OpenAI)."""
    if not base_url:
        return True
    host = (urlparse(base_url).hostname or "").lower()
    return host == "openai.com" or host.endswith((".openai.com", ".openai.azure.com"))


class ModelClient:
    """Streaming model client built on the Responses API.

    Which endpoint is called:

    - ``/v1/responses`` by default, on OpenAI and on any other base URL.
    - If a server other than OpenAI answers ``/v1/responses`` with 404 or 405,
      the client switches to ``/v1/chat/completions`` for that call and every
      later one. Servers such as Gemini's OpenAI-compatible endpoint, Cerebras
      and llama.cpp only implement Chat Completions.
    - ``api="responses"`` or ``api="chat"`` (or the ``RHO_AGENT_API``
      environment variable) pins the endpoint, with no fallback.

    Responses requests are stateless (``store=False``). The reasoning items a
    model returns come back on the ``done`` event as ``response_items``; the
    caller stores them on the assistant message and they are sent back, with
    their encrypted content, on later requests.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        service_tier: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        response_format: dict[str, Any] | None = None,
        api: str | None = None,
        streaming: bool = True,
        prices: Prices | None = None,
    ) -> None:
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._response_format = response_format
        # For flex processing, use longer timeout (15 min) per OpenAI docs
        if timeout is None:
            timeout = 900.0 if service_tier == "flex" else 60.0
        self._base_url = base_url or os.getenv(BASE_URL_ENV) or ""
        api_key = api_key or os.getenv(API_KEY_ENV)
        if not api_key:
            if not self._base_url:
                raise MissingApiKeyError()
            # A server of your own (vLLM, Ollama, ...) may need no key at all.
            api_key = "none"
        # Both are always passed, so the SDK never falls back to OPENAI_API_KEY
        # or OPENAI_BASE_URL from the environment.
        self._client = AsyncOpenAI(
            base_url=self._base_url or OPENAI_ENDPOINT,
            api_key=api_key,
            timeout=timeout,
            max_retries=8,
        )
        self._model = model
        self._service_tier = service_tier
        self._streaming = streaming
        # Explicit prices win; otherwise the environment, then the built-in table.
        self._prices = prices if prices is not None else prices_from_env()
        # OpenRouter reports each call's cost and groups calls by session ID.
        self._openrouter = _is_openrouter_host(self._base_url)

        self._send_include = True
        requested = _requested_api(api)
        self._chat: Any | None = None
        # Only a non-OpenAI server with no pinned endpoint may lack /v1/responses.
        self._can_fall_back = requested is None and not _is_openai_host(self._base_url)
        if requested == "chat":
            self._use_chat()

    @property
    def api(self) -> str:
        """The endpoint in use: ``"responses"`` or ``"chat"``."""
        return "chat" if self._chat is not None else "responses"

    def can_price(self) -> bool:
        """Whether calls to this model can be priced, so a cost cap can work."""
        return self._openrouter or can_price(self._model, self._prices)

    def _priced(self, usage: dict[str, Any]) -> dict[str, Any]:
        """``usage`` with ``cost_usd`` filled in from the token counts."""
        if "cost_usd" in usage:
            return usage  # the server reported the cost itself
        if not (usage.get("input_tokens") or usage.get("output_tokens")):
            return {**usage, "cost_usd": 0.0}
        cost = call_cost(self._model, usage, service_tier=self._service_tier, prices=self._prices)
        return {**usage, "cost_usd": cost or 0.0}

    def _use_chat(self) -> None:
        """Send this and every later call to Chat Completions."""
        from .chat_completions import ChatCompletionsClient

        self._chat = ChatCompletionsClient(
            self._client,
            self._model,
            base_url=self._base_url,
            service_tier=self._service_tier,
            temperature=self._temperature,
            reasoning_effort=self._reasoning_effort,
            response_format=self._response_format,
            send_session_id=self._openrouter,
        )
        self._can_fall_back = False

    def _fall_back(self, exc: BaseException) -> bool:
        """Switch to Chat Completions if ``exc`` says the server has no Responses API."""
        if not self._can_fall_back:
            return False
        if getattr(exc, "status_code", None) not in _NO_ENDPOINT_STATUSES:
            return False
        self._use_chat()
        return True

    async def _create(self, **kwargs: Any) -> Any:
        """Call ``responses.create``, dropping ``include`` if the server rejects it.

        Some servers (Groq, for one) implement Responses but not ``include``.
        Without it, reasoning comes back unencrypted or not at all, and the
        session still works.
        """
        if not self._send_include:
            kwargs.pop("include", None)
        try:
            return await self._client.responses.create(**kwargs)
        except APIStatusError as e:
            if "include" not in kwargs or e.status_code != 400 or "'include'" not in str(e):
                raise
            self._send_include = False
            kwargs.pop("include")
            return await self._client.responses.create(**kwargs)

    def _request(
        self,
        input_items: list[dict[str, Any]],
        *,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        structured: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Keyword arguments for ``responses.create``."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            # Stateless: nothing is kept on OpenAI's side, and reasoning comes
            # back encrypted so it can be replayed from the local history.
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            kwargs["instructions"] = instructions
        # Reasoning models reject temperature, so an effort takes precedence.
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        elif self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if tools:
            kwargs["tools"] = to_response_tools(tools)
            if tool_choice:
                kwargs["tool_choice"] = to_response_tool_choice(tool_choice)
        if self._service_tier:
            kwargs["service_tier"] = self._service_tier
        if structured and self._response_format:
            kwargs["text"] = {"format": to_response_text_format(self._response_format)}
        if self._openrouter and session_id:
            kwargs["extra_headers"] = {"x-session-id": session_id}
        return kwargs

    def _prompt_request(self, prompt: Prompt, *, structured: bool = True) -> dict[str, Any]:
        return self._request(
            to_response_input(prompt.messages),
            instructions=prompt.system,
            tools=prompt.tools,
            tool_choice=prompt.tool_choice,
            structured=structured,
            session_id=prompt.session_id,
        )

    async def stream(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """Stream a response from the model. The ``done`` event carries the cost."""
        async for event in self._stream_events(prompt):
            if event.type == "done" and event.usage is not None:
                event.usage = self._priced(event.usage)
            yield event

    async def _stream_events(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        if self._chat is not None:
            async for event in self._chat.stream(prompt):
                yield event
            return
        if not self._streaming:
            async for event in self._stream_via_complete(prompt):
                yield event
            return

        kwargs = self._prompt_request(prompt)
        kwargs["stream"] = True
        items: list[dict[str, Any]] = []
        text_item_id: str | None = None

        try:
            try:
                stream = await self._create(**kwargs)
            except APIStatusError as e:
                if not self._fall_back(e):
                    raise
                async for event in self._chat.stream(prompt):
                    yield event
                return
            try:
                async for event in stream:
                    kind = event.type
                    if kind in ("response.output_text.delta", "response.refusal.delta"):
                        if not event.delta:
                            continue
                        item_id = getattr(event, "item_id", None)
                        if text_item_id is not None and item_id != text_item_id:
                            # A second message in one response (commentary,
                            # then the answer): keep them apart.
                            yield StreamEvent(type="text", content="\n\n")
                        text_item_id = item_id
                        yield StreamEvent(type="text", content=event.delta)
                    elif kind == "response.output_item.done":
                        # Only the finished item is used: its arguments and
                        # encrypted reasoning are complete here and nowhere earlier.
                        tool_call = _record_item(event.item, items)
                        if tool_call is not None:
                            yield StreamEvent(type="tool_call", tool_call=tool_call)
                    elif kind in ("response.completed", "response.incomplete"):
                        yield StreamEvent(
                            type="done",
                            usage=_usage(event.response),
                            stop_reason=_stop_reason(event.response),
                            response_items=_items_to_store(items),
                        )
                    elif kind == "response.failed":
                        error = getattr(event.response, "error", None)
                        yield _response_error(
                            getattr(error, "code", None), getattr(error, "message", None)
                        )
                    elif kind == "error":
                        yield _response_error(event.code, event.message)
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    await close()
        except APIStatusError as e:
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
                error_kind=classify_error(e),
            )
        except Exception as e:
            yield StreamEvent(type="error", content=str(e), error_kind=classify_error(e))

    async def _stream_via_complete(self, prompt: Prompt) -> AsyncIterator[StreamEvent]:
        """One non-streaming request, reported as the same events as ``stream``.

        Used with ``streaming=False``, for accounts or gateways that cannot
        stream a model.
        """
        try:
            response = await self._create(**self._prompt_request(prompt))
        except APIStatusError as e:
            if self._fall_back(e):
                async for event in self._chat.stream(prompt):
                    yield event
                return
            yield StreamEvent(
                type="error",
                content=f"API error {e.status_code} (after retries): {e.message}",
                error_kind=classify_error(e),
            )
            return
        except Exception as e:
            yield StreamEvent(type="error", content=str(e), error_kind=classify_error(e))
            return

        error = getattr(response, "error", None)
        if error is not None:
            yield _response_error(getattr(error, "code", None), getattr(error, "message", None))
            return

        items: list[dict[str, Any]] = []
        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in response.output:
            tool_call = _record_item(item, items)
            if tool_call is not None:
                tool_calls.append(tool_call)
            elif getattr(item, "type", None) == "message" and _message_text(item):
                texts.append(_message_text(item))
        if texts:
            yield StreamEvent(type="text", content="\n\n".join(texts))
        for tool_call in tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tool_call)
        yield StreamEvent(
            type="done",
            usage=_usage(response),
            stop_reason=_stop_reason(response),
            response_items=_items_to_store(items),
        )

    async def _create_text(self, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Run one non-streaming request and return ``(text, usage)``."""
        response = await self._create(**kwargs)
        error = getattr(response, "error", None)
        if error is not None:
            failure = _response_error(getattr(error, "code", None), getattr(error, "message", None))
            raise ModelCallError(
                failure.content or "response failed", failure.error_kind or "fatal"
            )
        text = "\n\n".join(
            _message_text(item)
            for item in response.output
            if getattr(item, "type", None) == "message" and _message_text(item)
        )
        return text, _usage(response)

    async def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """Non-streaming completion of chat-format messages: ``(content, usage_dict)``.

        A failure is returned as the content, not raised.
        """
        text, usage = await self._complete(messages)
        return text, self._priced(usage)

    async def _complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        if self._chat is not None:
            return await self._chat.complete(messages)
        history = [
            Message(
                role=m["role"],
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                response_items=m.get("response_items"),
            )
            for m in messages
        ]
        try:
            return await self._create_text(self._request(to_response_input(history)))
        except APIStatusError as e:
            if self._fall_back(e):
                return await self._chat.complete(messages)
            return f"API error {e.status_code} (after retries): {e.message}", dict(_ZERO_USAGE)
        except Exception as e:
            return f"Error: {e}", dict(_ZERO_USAGE)

    async def complete_prompt(self, prompt: Prompt) -> tuple[str, dict[str, Any]]:
        """Non-streaming completion from a Prompt, preserving system/tools prefix.

        Like complete() but accepts a Prompt so the system prompt and tool
        definitions are included in the request — enabling prompt-cache reuse
        when the prefix matches a previous streaming call. The configured
        ``response_format`` is not applied: callers want plain text.

        Raises:
            ModelCallError: if the call fails. The error text is never returned
                as content, because callers use the content as a summary.
        """
        text, usage = await self._complete_prompt(prompt)
        return text, self._priced(usage)

    async def _complete_prompt(self, prompt: Prompt) -> tuple[str, dict[str, Any]]:
        if self._chat is not None:
            return await self._chat.complete_prompt(prompt)
        try:
            return await self._create_text(self._prompt_request(prompt, structured=False))
        except ModelCallError:
            raise
        except APIStatusError as e:
            if self._fall_back(e):
                return await self._chat.complete_prompt(prompt)
            raise ModelCallError(
                f"API error {e.status_code} (after retries): {e.message}", classify_error(e)
            ) from e
        except Exception as e:
            raise ModelCallError(str(e), classify_error(e)) from e
