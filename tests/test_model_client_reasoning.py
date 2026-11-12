"""ModelClient: models that refuse function tools together with reasoning."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from openai import APIStatusError

from rho_agent.client.model import ModelCallError, ModelClient, Prompt

REFUSAL = (
    "Function tools with reasoning_effort are not supported for gpt-6.1-terra in "
    "/v1/chat/completions. To use function tools, use /v1/responses or set "
    "reasoning_effort to 'none'."
)
TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]


class _Refusal(APIStatusError):
    """The 400 the API returns, built without the SDK's HTTP response type."""

    def __init__(self) -> None:
        Exception.__init__(self, REFUSAL)
        self.message = REFUSAL
        self.status_code = 400
        self.response = None
        self.body = None


def _refusal() -> APIStatusError:
    return _Refusal()


def _reply() -> Any:
    message = SimpleNamespace(content="ok", tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _client(create: AsyncMock, **kwargs: Any) -> ModelClient:
    client = ModelClient(model="gpt-6.1-terra", api_key="test-key", **kwargs)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client


def _prompt(tools: list[dict[str, Any]] | None = TOOLS) -> Prompt:
    return Prompt(system="s", messages=[], tools=tools)


@pytest.mark.asyncio
async def test_tool_request_is_retried_with_reasoning_off() -> None:
    create = AsyncMock(side_effect=[_refusal(), _reply(), _reply()])
    client = _client(create)

    text, _ = await client.complete_prompt(_prompt())
    await client.complete_prompt(_prompt())

    assert text == "ok"
    efforts = [call.kwargs.get("reasoning_effort") for call in create.call_args_list]
    # Refused, retried with reasoning off, and off from the start the next time.
    assert efforts == [None, "none", "none"]


@pytest.mark.asyncio
async def test_requests_without_tools_keep_reasoning() -> None:
    create = AsyncMock(side_effect=[_refusal(), _reply(), _reply()])
    client = _client(create)
    await client.complete_prompt(_prompt())

    await client.complete_prompt(_prompt(tools=None))

    assert "reasoning_effort" not in create.call_args_list[-1].kwargs


@pytest.mark.asyncio
async def test_configured_effort_is_not_overridden() -> None:
    create = AsyncMock(side_effect=[_refusal()])
    client = _client(create, reasoning_effort="high")

    with pytest.raises(ModelCallError, match="reasoning_effort"):
        await client.complete_prompt(_prompt())

    assert create.call_count == 1
