"""Model discovery and switching without live credentials."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from rho_agent.cli.events import ApprovalHandler
from rho_agent.cli.interactive import InteractiveSession
from rho_agent.cli.model_picker import pick_model
from rho_agent.client.catalog import CatalogModel
from rho_agent.client.model import ModelClient
from rho_agent.client.pricing import table_prices
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session_store import SessionStore


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://openrouter.ai/api/v1", (0.0, 7.5)),
        ("https://api.openai.com/v1", (table_prices("gpt-5").input, table_prices("gpt-5").output)),
        ("https://example.test/v1", (None, None)),
    ],
)
async def test_discovery_uses_active_connection(monkeypatch, base_url, expected):
    client = ModelClient(base_url=base_url, api_key="test-key")
    get = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "gpt-5",
                    "name": "GPT-5",
                    "pricing": {"prompt": "0", "completion": "0.0000075"},
                    "context_length": 400_000,
                    "supported_parameters": ["tools"],
                }
            ]
        }
    )
    monkeypatch.setattr(client._client, "get", get)
    try:
        [model] = await client.list_models()
        assert model.id == "gpt-5"
        assert (model.input_price, model.output_price) == expected
        assert model.context_window == 400_000
        assert model.supports_tools
        get.assert_awaited_once()
        assert get.await_args.args == ("/models",)
        assert client._client.base_url.host in base_url
    finally:
        await client._client.close()


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("beta\r", "beta"),
        ("\x1bOQ\r", "beta"),
        ("\x1bOR\r", "alpha"),
        ("\x1bOS\r", "aaa"),
        ("\x03", None),
    ],
)
async def test_picker_keyboard(keys, expected):
    models = [
        CatalogModel("alpha", input_price=3, output_price=1, supports_tools=True),
        CatalogModel("beta", input_price=1, output_price=2, supports_tools=True),
        CatalogModel("aaa", input_price=0, output_price=0),
    ]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        result = await asyncio.wait_for(
            pick_model(models, current="alpha", openrouter=True), timeout=2
        )
    assert (result.id if result else None) == expected


async def test_repl_selection_preserves_history_and_updates_transport(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RHO_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("RHO_AGENT_API", "chat")
    agent = Agent(
        AgentConfig(
            model="old",
            base_url="https://openrouter.ai/api/v1",
            working_dir=str(tmp_path),
            access="full",
        )
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(agent)
    session.state.add_user_message("question")
    session.state.add_assistant_message(
        "answer", response_items=[{"type": "reasoning", "encrypted_content": "old-data"}]
    )
    session.state.update_usage(input_tokens=10)
    transport = session._client._client
    model = CatalogModel("new", context_window=32_768)
    monkeypatch.setattr(session, "list_models", AsyncMock(return_value=[model]))
    monkeypatch.setattr("rho_agent.cli.interactive.pick_model", AsyncMock(return_value=model))
    repl = InteractiveSession(session, ApprovalHandler(), str(tmp_path), store)

    try:
        await repl._handle_model("/model")

        assert session.agent.config.model == "new"
        assert session._client._chat._model == "new"
        assert session._client._client is transport
        assert session.state.messages == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        assert repl.token_status.total_input_tokens == 10
        assert ("class:toolbar.model", "new") in repl.token_status.render_toolbar()
        assert repl.token_status.context_window == session.context_window == 32_768
        assert store.load_config(session.id).model == "new"
        assert store.list()[0].model == "new"
    finally:
        await transport.close()
