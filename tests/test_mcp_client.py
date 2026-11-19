"""MCP servers as agent tools: config parsing, result conversion, and live servers."""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rho_agent.core import Agent, AgentConfig
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.mcp_client import (
    MCPServerConfig,
    MCPServers,
    parse_mcp_servers,
    result_to_output,
    tool_name,
)
from rho_agent.tools.registry import ToolRegistry

SERVER = Path(__file__).parent / "fixtures" / "mcp_test_server.py"


# --- config ---


def test_parse_servers_from_json_and_normalise_http() -> None:
    servers = parse_mcp_servers(
        '[{"name": "pw", "transport": "sse", "url": "http://playwright-mcp:3080/sse"},'
        ' {"name": "api", "transport": "http", "url": "http://x/mcp",'
        '  "headers": {"Authorization": "Bearer ${TOKEN}"}},'
        ' {"name": "local", "transport": "stdio", "command": "npx", "args": ["srv", 1]}]'
    )
    assert [s.transport for s in servers] == ["sse", "streamable-http", "stdio"]
    assert servers[0].url == "http://playwright-mcp:3080/sse"
    assert servers[1].headers == {"Authorization": "Bearer ${TOKEN}"}
    assert servers[2].args == ["srv", "1"]
    assert parse_mcp_servers(None) == [] and parse_mcp_servers("  ") == []
    assert MCPServerConfig.from_dict(servers[1].to_dict()) == servers[1]


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"transport": "sse", "url": "http://x"}, "needs a 'name'"),
        ({"name": "a", "transport": "websocket", "url": "ws://x"}, "unknown transport"),
        ({"name": "a", "transport": "sse"}, "'url' is required"),
        ({"name": "a", "transport": "stdio"}, "'command' is required"),
    ],
)
def test_invalid_server_entries_are_rejected(entry: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_mcp_servers([entry])


def test_duplicate_names_and_bad_json_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        parse_mcp_servers([{"name": "a", "url": "http://x"}, {"name": "a", "url": "http://y"}])
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_mcp_servers("[{")
    with pytest.raises(ValueError, match="'url' is required"):
        AgentConfig(mcp_servers=[{"name": "a", "transport": "sse"}])


def test_agent_config_keeps_servers_as_plain_mappings(tmp_path: Path) -> None:
    config = AgentConfig(mcp_servers=[{"name": "pw", "transport": "http", "url": "http://x/mcp"}])
    assert config.mcp_servers == [
        {"name": "pw", "transport": "streamable-http", "url": "http://x/mcp"}
    ]
    path = tmp_path / "agent.yaml"
    config.to_file(path)
    assert AgentConfig.from_file(path).mcp_servers == config.mcp_servers


def test_tool_names_are_namespaced_and_api_safe() -> None:
    assert tool_name("playwright", "browser_click") == "mcp__playwright__browser_click"
    assert tool_name("my server", "screen.shot") == "mcp__my_server__screen_shot"
    long = tool_name("s" * 40, "t" * 40)
    assert len(long) == 64 and long.startswith("mcp__sss")
    assert long != tool_name("s" * 40, "t" * 39 + "u")


# --- result conversion ---


def _content(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_result_text_images_and_resources() -> None:
    result = SimpleNamespace(
        isError=False,
        structuredContent=None,
        content=[
            _content("text", text="clicked"),
            _content("image", data=PNG_B64, mimeType="image/png"),
            _content(
                "resource",
                resource=SimpleNamespace(uri="file:///a.txt", text="hello", mimeType="text/plain"),
            ),
            _content("resource", resource=SimpleNamespace(uri="file:///b.bin", mimeType=None)),
            _content("resource_link", uri="file:///c"),
            _content("audio", data="", mimeType="audio/wav"),
        ],
    )
    output = result_to_output(result, label="mcp__pw__shot")
    assert output.success
    assert "clicked" in output.content
    assert "[image 1 attached" in output.content
    assert "[resource file:///a.txt]\nhello" in output.content
    assert "file:///b.bin (binary) omitted" in output.content
    assert "[resource link: file:///c]" in output.content
    assert "audio (audio/wav) omitted" in output.content
    assert output.images == [
        {"path": "mcp__pw__shot image 1", "media_type": "image/png", "data": PNG_B64}
    ]

    no_images = result_to_output(result, label="x", attach_images=False)
    assert no_images.images == [] and "image viewing is off" in no_images.content


def test_result_errors_and_structured_content() -> None:
    error = result_to_output(
        SimpleNamespace(isError=True, content=[], structuredContent=None), label="x"
    )
    assert not error.success and error.content == "(error with no message)"
    structured = result_to_output(
        SimpleNamespace(isError=False, content=[], structuredContent={"total": 3}), label="x"
    )
    assert '"total": 3' in structured.content


# --- live servers ---

needs_mcp = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None, reason="mcp extra not installed"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server(request: pytest.FixtureRequest):
    """Start the test server over an HTTP transport; yields its URL."""
    transport = request.param
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), transport, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    path = "/sse" if transport == "sse" else "/mcp"
    try:
        yield transport, f"http://127.0.0.1:{port}{path}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _stdio_config(name: str = "calc") -> dict[str, Any]:
    return {"name": name, "transport": "stdio", "command": sys.executable, "args": [str(SERVER)]}


async def _call(registry: ToolRegistry, name: str, **arguments: Any):
    return await registry.dispatch(
        ToolInvocation(call_id="c1", tool_name=name, arguments=arguments)
    )


@needs_mcp
async def test_stdio_server_tools_are_registered_and_called() -> None:
    registry = ToolRegistry()
    servers = MCPServers(parse_mcp_servers([_stdio_config()]), connect_timeout=30)
    await servers.connect(registry)
    try:
        assert servers.failed == {}
        assert sorted(servers.registered) == [
            "mcp__calc__add",
            "mcp__calc__explode",
            "mcp__calc__screen_shot",
            "mcp__calc__whoami",
        ]
        [spec] = [s for s in registry.get_specs() if s["function"]["name"] == "mcp__calc__add"]
        assert spec["function"]["description"] == "[MCP server 'calc'] Add two integers."
        params = spec["function"]["parameters"]
        assert params["type"] == "object" and set(params["required"]) == {"a", "b"}

        # Arguments are coerced from the schema, as for built-in tools.
        added = await _call(registry, "mcp__calc__add", a="2", b=3)
        assert added.success and added.content == "5"

        shot = await _call(registry, "mcp__calc__screen_shot")
        assert shot.success and shot.images[0]["media_type"] == "image/png"

        failed = await _call(registry, "mcp__calc__explode")
        assert not failed.success and "kaboom" in failed.content
    finally:
        await servers.close(registry)
    assert "mcp__calc__add" not in registry


@needs_mcp
@pytest.mark.parametrize("http_server", ["sse", "streamable-http"], indirect=True)
async def test_http_servers_with_headers(
    http_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, url = http_server
    monkeypatch.setenv("TEST_MCP_TOKEN", "tok-123")
    config = {
        "name": "web",
        "transport": transport,
        "url": url,
        "headers": {"X-Test-Token": "${TEST_MCP_TOKEN}"},
    }
    registry = ToolRegistry()
    servers = MCPServers(parse_mcp_servers([config]), connect_timeout=30)
    await servers.connect(registry)
    try:
        assert servers.failed == {}
        assert (await _call(registry, "mcp__web__add", a=1, b=1)).content == "2"
        assert (await _call(registry, "mcp__web__whoami")).content == "tok-123"
    finally:
        await servers.close(registry)


@needs_mcp
async def test_unreachable_and_misconfigured_servers_are_skipped() -> None:
    registry = ToolRegistry()
    configs = parse_mcp_servers(
        [
            {"name": "down", "transport": "sse", "url": f"http://127.0.0.1:{_free_port()}/sse"},
            {
                "name": "nokey",
                "transport": "streamable-http",
                "url": "http://127.0.0.1:1/mcp",
                "headers": {"Authorization": "Bearer ${RHO_TEST_UNSET_VARIABLE}"},
            },
            _stdio_config(),
        ]
    )
    servers = MCPServers(configs, connect_timeout=3)
    await servers.connect(registry)
    try:
        assert set(servers.failed) == {"down", "nokey"}
        assert "RHO_TEST_UNSET_VARIABLE is not set" in servers.failed["nokey"]
        assert "mcp__calc__add" in registry  # the working server is still used
    finally:
        await servers.close(registry)


async def test_missing_sdk_skips_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    from rho_agent.tools import mcp_client

    def missing() -> None:
        raise mcp_client.MCPUnavailableError("MCP servers need the 'mcp' package")

    monkeypatch.setattr(mcp_client, "_import_mcp", missing)
    servers = MCPServers(parse_mcp_servers([_stdio_config()]))
    await servers.connect(ToolRegistry())
    assert "need the 'mcp' package" in servers.failed["calc"]


@needs_mcp
async def test_agent_connects_once_and_session_close_disconnects() -> None:
    from rho_agent.core import Session

    agent = Agent(AgentConfig(tools=["bash"], mcp_servers=[_stdio_config()]))
    assert "mcp__calc__add" not in agent.registry
    first = await agent.connect_mcp()
    assert await agent.connect_mcp() is first
    assert "mcp__calc__add" in agent.registry
    assert first is not None and first.attach_images is False  # view_image not in tools

    await Session(agent, client=object()).close()
    assert "mcp__calc__add" not in agent.registry
    assert await Agent(AgentConfig()).connect_mcp() is None


@needs_mcp
async def test_dropped_connection_reconnects_on_the_next_call() -> None:
    registry = ToolRegistry()
    servers = MCPServers(parse_mcp_servers([_stdio_config()]), connect_timeout=30)
    await servers.connect(registry)
    try:
        connection = servers.connections["calc"]
        # Simulate the server going away.
        task = connection._task
        assert task is not None
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        assert not connection.connected
        assert (await _call(registry, "mcp__calc__add", a=4, b=5)).content == "9"
    finally:
        await servers.close(registry)
