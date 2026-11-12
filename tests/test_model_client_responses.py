"""ModelClient on the Responses API, with a faked SDK client (no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import APIStatusError

from rho_agent.client.model import (
    Message,
    ModelCallError,
    ModelClient,
    Prompt,
    StreamEvent,
    to_response_input,
    to_response_text_format,
    to_response_tool_choice,
    to_response_tools,
)
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


@pytest.fixture(autouse=True)
def _openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("RHO_AGENT_API", raising=False)


# ---------------------------------------------------------------------------
# Fakes for the SDK's Responses objects
# ---------------------------------------------------------------------------


def _usage(input_tokens: int = 100, output_tokens: int = 40, cached: int = 0, reasoning: int = 0):
    return NS(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=NS(cached_tokens=cached),
        output_tokens_details=NS(reasoning_tokens=reasoning),
    )


def _response(output: list[Any] | None = None, status: str = "completed", **kwargs: Any) -> NS:
    fields = {"usage": _usage(), "error": None, "incomplete_details": None, **kwargs}
    return NS(output=output or [], status=status, **fields)


def _reasoning(item_id: str = "rs_1", encrypted: str | None = "ENC1") -> NS:
    return NS(type="reasoning", id=item_id, summary=[], encrypted_content=encrypted)


def _message(text: str, item_id: str = "msg_1", phase: str | None = None) -> NS:
    return NS(type="message", id=item_id, phase=phase, content=[NS(type="output_text", text=text)])


def _function_call(call_id: str, name: str, arguments: str) -> NS:
    return NS(
        type="function_call", id=f"fc_{call_id}", call_id=call_id, name=name, arguments=arguments
    )


def _item_done(item: NS) -> NS:
    return NS(type="response.output_item.done", item=item)


def _text_delta(delta: str, item_id: str = "msg_1") -> NS:
    return NS(type="response.output_text.delta", delta=delta, item_id=item_id)


def _args_delta(delta: str) -> NS:
    return NS(type="response.function_call_arguments.delta", delta=delta, item_id="fc_1")


def _completed(**kwargs: Any) -> NS:
    return NS(type="response.completed", response=_response(**kwargs))


class _FakeStream:
    def __init__(self, events: list[NS]) -> None:
        self._events = events
        self.closed = False

    async def __aiter__(self):
        for event in self._events:
            yield event

    async def close(self) -> None:
        self.closed = True


class _FakeResponses:
    """Stands in for ``AsyncOpenAI().responses``; replays one result per call."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeStream(result) if isinstance(result, list) else result


class _StatusError(APIStatusError):
    """An HTTP error built without the SDK's response type."""

    def __init__(self, status_code: int, message: str) -> None:
        Exception.__init__(self, message)
        self.message = message
        self.status_code = status_code
        self.response = None
        self.body = None


def _client(*results: Any, **kwargs: Any) -> tuple[ModelClient, _FakeResponses]:
    client = ModelClient(model="gpt-6.1-terra", api_key="test-key", **kwargs)
    responses = _FakeResponses(*results)
    client._client = NS(responses=responses)
    return client, responses


async def _collect(client: ModelClient, prompt: Prompt) -> list[StreamEvent]:
    return [event async for event in client.stream(prompt)]


def _prompt(*messages: Message, tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> Prompt:
    return Prompt(system="sys", messages=list(messages), tools=tools or [], **kwargs)


# ---------------------------------------------------------------------------
# Conversion of history, tools and options
# ---------------------------------------------------------------------------


def test_history_converts_to_input_items() -> None:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read", "arguments": '{"path": "a"}'},
    }
    items = to_response_input(
        [
            Message(role="user", content="hello"),
            Message(role="assistant", content="Looking.", tool_calls=[call]),
            Message(role="tool", tool_call_id="call_1", content="file text"),
            Message(role="assistant", content="Done."),
        ]
    )

    assert items == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Looking."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"path": "a"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "file text"},
        {"role": "assistant", "content": "Done."},
    ]


def test_image_parts_convert_to_input_images() -> None:
    content = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "low"}},
    ]

    [item] = to_response_input([Message(role="user", content=content)])

    assert item == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
            {"type": "input_image", "image_url": "https://x/y.png", "detail": "low"},
        ],
    }


def test_response_items_replay_in_the_models_order() -> None:
    reasoning_1 = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E1"}
    reasoning_2 = {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "E2"}
    said = {"type": "message", "role": "assistant", "content": "Checking.", "phase": "commentary"}
    call = {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    extra = {"id": "call_9", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    message = Message(
        role="assistant",
        content="Checking.",
        tool_calls=[call, extra],
        response_items=[
            reasoning_1,
            said,
            reasoning_2,
            {"type": "function_call", "call_id": "call_1"},
            # A call that is no longer in tool_calls is not sent.
            {"type": "function_call", "call_id": "call_gone"},
        ],
    )

    items = to_response_input([message])

    assert items[:3] == [reasoning_1, said, reasoning_2]
    assert [i["call_id"] for i in items[3:]] == ["call_1", "call_9"]
    assert all(i["type"] == "function_call" and i["name"] == "read" for i in items[3:])


def test_tools_convert_to_responses_format() -> None:
    strict = {"type": "function", "function": {"name": "s", "parameters": {}, "strict": True}}

    plain, strict_tool = to_response_tools([TOOLS[0], strict])

    assert plain == {
        "type": "function",
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        # Responses treats a missing ``strict`` as true, which would reject this schema.
        "strict": False,
    }
    assert strict_tool["strict"] is True


def test_tool_choice_and_response_format_convert() -> None:
    assert to_response_tool_choice("none") == "none"
    assert to_response_tool_choice({"type": "function", "function": {"name": "read"}}) == {
        "type": "function",
        "name": "read",
    }
    schema = {"type": "object", "properties": {}}
    chat_format = {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": schema, "strict": True},
    }
    assert to_response_text_format(chat_format) == {
        "type": "json_schema",
        "name": "answer",
        "schema": schema,
        "strict": True,
    }
    assert to_response_text_format({"type": "json_object"}) == {"type": "json_object"}


async def test_request_carries_options() -> None:
    client, responses = _client(
        [_completed()],
        reasoning_effort="low",
        service_tier="flex",
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    await _collect(
        client, _prompt(Message(role="user", content="hi"), tools=TOOLS, tool_choice="none")
    )

    [request] = responses.requests
    assert request["model"] == "gpt-6.1-terra"
    assert request["instructions"] == "sys"
    assert request["input"] == [{"role": "user", "content": "hi"}]
    assert request["stream"] is True
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["reasoning"] == {"effort": "low"}
    assert "temperature" not in request  # reasoning models reject it
    assert request["service_tier"] == "flex"
    assert request["tool_choice"] == "none"
    assert request["tools"][0]["name"] == "read"
    assert request["text"] == {"format": {"type": "json_object"}}


async def test_temperature_is_sent_without_reasoning_effort() -> None:
    client, responses = _client([_completed()], temperature=0.2)

    await _collect(client, _prompt())

    assert responses.requests[0]["temperature"] == 0.2
    assert "reasoning" not in responses.requests[0]
    assert "tools" not in responses.requests[0]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_stream_assembles_text_tool_calls_and_usage() -> None:
    client, _ = _client(
        [
            _item_done(_reasoning()),
            _text_delta("Look"),
            _text_delta("ing."),
            _item_done(_message("Looking.")),
            # Argument fragments are ignored: the finished item has them whole.
            _args_delta('{"pa'),
            _args_delta('th": "a.txt"}'),
            _item_done(_function_call("call_1", "read", '{"path": "a.txt"}')),
            _item_done(_function_call("call_2", "read", '{"path": ')),
            _completed(usage=_usage(input_tokens=120, output_tokens=60, cached=80, reasoning=35)),
        ]
    )

    events = await _collect(client, _prompt(tools=TOOLS))

    assert [e.type for e in events] == ["text", "text", "tool_call", "tool_call", "done"]
    assert "".join(e.content or "" for e in events if e.type == "text") == "Looking."
    first, second = (e.tool_call for e in events if e.type == "tool_call")
    assert (first.id, first.name, first.arguments, first.parse_error) == (
        "call_1",
        "read",
        {"path": "a.txt"},
        None,
    )
    assert second.arguments == {} and "not valid JSON" in second.parse_error
    done = events[-1]
    assert done.usage == {
        "input_tokens": 120,
        "output_tokens": 60,
        "cached_tokens": 80,
        "reasoning_tokens": 35,
    }
    assert done.response_items == [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"},
        {"type": "message", "role": "assistant", "content": "Looking."},
        {"type": "function_call", "call_id": "call_1"},
        {"type": "function_call", "call_id": "call_2"},
    ]


async def test_response_without_reasoning_stores_no_items() -> None:
    client, _ = _client(
        [
            _text_delta("hi"),
            _item_done(_message("hi")),
            # Reasoning that cannot be replayed statelessly is dropped.
            _item_done(_reasoning(encrypted=None)),
            _completed(),
        ]
    )

    events = await _collect(client, _prompt())

    assert events[-1].type == "done" and events[-1].response_items is None


async def test_two_messages_in_one_response_are_separated() -> None:
    client, _ = _client(
        [
            _text_delta("First.", item_id="msg_1"),
            _text_delta("Second.", item_id="msg_2"),
            _completed(),
        ]
    )

    events = await _collect(client, _prompt())

    assert "".join(e.content or "" for e in events if e.type == "text") == "First.\n\nSecond."


async def test_incomplete_response_reports_the_reason() -> None:
    response = _response(status="incomplete", incomplete_details=NS(reason="max_output_tokens"))
    client, _ = _client([_text_delta("par"), NS(type="response.incomplete", response=response)])

    events = await _collect(client, _prompt())

    assert events[-1].type == "done" and events[-1].stop_reason == "max_output_tokens"


@pytest.mark.parametrize(
    ("events", "kind"),
    [
        (
            [
                NS(
                    type="response.failed",
                    response=_response(error=NS(code="server_error", message="x")),
                )
            ],
            "transient",
        ),
        ([NS(type="error", code="invalid_prompt", message="bad")], "fatal"),
        ([NS(type="error", code=None, message="context_length_exceeded")], "context_overflow"),
    ],
)
async def test_failures_inside_the_stream_become_error_events(events: list[NS], kind: str) -> None:
    client, _ = _client(events)

    [event] = await _collect(client, _prompt())

    assert event.type == "error" and event.error_kind == kind


@pytest.mark.parametrize(("status", "kind"), [(400, "fatal"), (429, "transient")])
async def test_http_errors_become_error_events(status: int, kind: str) -> None:
    client, _ = _client(_StatusError(status, "nope"))

    [event] = await _collect(client, _prompt())

    assert event.type == "error" and event.error_kind == kind
    assert f"API error {status}" in event.content


async def test_non_streaming_mode_yields_the_same_events() -> None:
    output = [
        _reasoning(),
        _message("Looking."),
        _function_call("call_1", "read", '{"path": "a.txt"}'),
    ]
    client, responses = _client(_response(output, usage=_usage(reasoning=7)), streaming=False)

    events = await _collect(client, _prompt(tools=TOOLS))

    assert "stream" not in responses.requests[0]
    assert [e.type for e in events] == ["text", "tool_call", "done"]
    assert events[0].content == "Looking."
    assert events[1].tool_call.arguments == {"path": "a.txt"}
    assert events[2].usage["reasoning_tokens"] == 7
    assert [i["type"] for i in events[2].response_items] == [
        "reasoning",
        "message",
        "function_call",
    ]


# ---------------------------------------------------------------------------
# complete() and complete_prompt()
# ---------------------------------------------------------------------------


async def test_complete_prompt_returns_text_and_usage() -> None:
    reply = _response(
        [_reasoning(), _message("summary")], usage=_usage(10, 5, cached=4, reasoning=2)
    )
    client, responses = _client(reply, response_format={"type": "json_object"})

    text, usage = await client.complete_prompt(_prompt(tools=TOOLS, tool_choice="none"))

    assert text == "summary"
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
        "cost_usd": 0.0,
    }
    [request] = responses.requests
    assert request["tool_choice"] == "none" and "stream" not in request
    assert "text" not in request  # summaries are plain text


async def test_complete_prompt_raises_on_failure() -> None:
    client, _ = _client(_StatusError(400, "context_length_exceeded"))

    with pytest.raises(ModelCallError) as raised:
        await client.complete_prompt(_prompt())

    assert raised.value.kind == "context_overflow"


async def test_complete_converts_chat_messages_and_returns_errors_as_text() -> None:
    client, responses = _client(_response([_message("fine")]), _StatusError(500, "down"))
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]

    text, _ = await client.complete(messages)
    failed, usage = await client.complete(messages)

    assert text == "fine"
    assert responses.requests[0]["input"] == messages
    assert failed.startswith("API error 500") and usage["input_tokens"] == 0


# ---------------------------------------------------------------------------
# Reasoning round trip through a session and its trace
# ---------------------------------------------------------------------------


class _ReadHandler(ToolHandler):
    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "Read a file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        return ToolOutput(content="file text")


def _session(client: ModelClient, state: State) -> Session:
    registry = ToolRegistry()
    registry.register(_ReadHandler())
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(system_prompt="sys", profile="readonly", model="gpt-6.1-terra")
    agent.system_prompt = "sys"
    agent.registry = registry
    agent.sandbox_manager = None
    return Session(agent, client=client, state=state)


async def test_reasoning_items_go_back_on_the_next_request_and_after_resume(
    tmp_path: Path,
) -> None:
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"}
    trace = tmp_path / "trace.jsonl"
    client, responses = _client(
        [
            _item_done(_reasoning()),
            _item_done(_function_call("call_1", "read", '{"path": "a.txt"}')),
            _completed(usage=_usage(reasoning=30)),
        ],
        [
            _item_done(_reasoning("rs_2", "ENC2")),
            _text_delta("It says: file text"),
            _item_done(_message("It says: file text")),
            _completed(usage=_usage(reasoning=12)),
        ],
        [_text_delta("Again."), _item_done(_message("Again.")), _completed()],
    )
    session = _session(client, State(trace_path=trace))

    await session.run("read a.txt")

    # The second request replays the reasoning item ahead of the call it led to.
    assert responses.requests[1]["input"] == [
        {"role": "user", "content": "read a.txt"},
        reasoning,
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"path": "a.txt"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "file text"},
    ]
    assert session.state.usage["reasoning_tokens"] == 42

    # A session rebuilt from the trace file sends the same history again,
    # reasoning for the final answer included.
    resumed_state = State.from_jsonl(trace.read_bytes())
    assert resumed_state.usage["reasoning_tokens"] == 42
    resumed = _session(client, resumed_state)

    await resumed.run("and again")

    assert responses.requests[2]["input"] == [
        *responses.requests[1]["input"],
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "ENC2"},
        {"type": "message", "role": "assistant", "content": "It says: file text"},
        {"role": "user", "content": "and again"},
    ]


# ---------------------------------------------------------------------------
# Endpoint choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "api", "expected"),
    [
        (None, None, "responses"),
        ("https://api.openai.com/v1", None, "responses"),
        ("https://example.openai.azure.com/openai/v1", None, "responses"),
        ("http://localhost:8000/v1", None, "chat"),
        ("https://api.cerebras.ai/v1", None, "chat"),
        ("https://gateway.example.com/v1", "responses", "responses"),
        (None, "chat", "chat"),
    ],
)
def test_endpoint_choice(base_url: str | None, api: str | None, expected: str) -> None:
    client = ModelClient(model="m", base_url=base_url, api_key="test-key", api=api)

    assert client.api == expected


def test_endpoint_choice_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    assert ModelClient(model="m", api_key="test-key").api == "chat"

    monkeypatch.setenv("RHO_AGENT_API", "responses")
    assert ModelClient(model="m", api_key="test-key").api == "responses"

    with pytest.raises(ValueError, match="api must be"):
        ModelClient(model="m", api_key="test-key", api="completions")


async def test_custom_base_url_uses_chat_completions() -> None:
    client = ModelClient(model="gpt-oss-120b", base_url="http://localhost:8000/v1", api_key="k")

    def delta(**kwargs: Any) -> NS:
        return NS(content=None, tool_calls=None, **kwargs)

    def chunk(d: NS, finish: str | None = None) -> NS:
        return NS(choices=[NS(delta=d, finish_reason=finish)], usage=None)

    def tool_delta(arguments: str, call_id: str | None = None, name: str | None = None) -> NS:
        function = NS(name=name, arguments=arguments)
        return NS(content=None, tool_calls=[NS(index=0, id=call_id, function=function)])

    chunks = [
        chunk(NS(content="Hi", tool_calls=None)),
        chunk(tool_delta('{"path"', "call_1", "read")),
        chunk(tool_delta(': "a"}')),
        chunk(delta(), finish="tool_calls"),
        NS(
            choices=[],
            usage=NS(prompt_tokens=9, completion_tokens=4, prompt_tokens_details=None),
        ),
    ]

    class _ChatStream(_FakeStream):
        async def __aenter__(self) -> _ChatStream:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    create = AsyncMock(return_value=_ChatStream(chunks))
    client._chat._client = NS(chat=NS(completions=NS(create=create)))
    history = Message(
        role="assistant",
        content="earlier",
        response_items=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E"}
        ],
    )

    events = await _collect(client, _prompt(history, tools=TOOLS))

    assert [e.type for e in events] == ["text", "tool_call", "done"]
    assert events[1].tool_call.arguments == {"path": "a"}
    request = create.call_args.kwargs
    # Chat-format tools and messages, without the Responses-only items.
    assert request["tools"] == TOOLS
    assert request["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "earlier"},
    ]
