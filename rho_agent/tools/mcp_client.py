"""MCP (Model Context Protocol) servers as agent tools.

Each configured server is connected, its tools are listed, and every tool is
registered in the agent's ToolRegistry as ``mcp__<server>__<tool>``. Calls are
routed to the server that owns the tool.

Needs the ``mcp`` extra (``uv pip install -e ".[mcp]"``); the SDK is imported
only when a server is configured. Supported transports: ``sse``,
``streamable-http`` (``http`` is accepted as an alias) and ``stdio``.

A server that cannot be reached is logged and left out; the agent keeps
running with the tools it has.

Config, as a list of mappings (YAML ``mcp_servers:`` or JSON in
``RHO_AGENT_MCP_SERVERS``)::

    - name: playwright
      transport: sse
      url: http://playwright-mcp:3080/sse
      headers: {Authorization: "Bearer ${PLAYWRIGHT_TOKEN}"}
    - name: files
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

``${VAR}`` in a header value is read from the environment when connecting.
``RHO_AGENT_MCP_CONNECT_TIMEOUT`` and ``RHO_AGENT_MCP_CALL_TIMEOUT`` (seconds)
override the connection and per-call time limits.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .base import ToolHandler, ToolInvocation, ToolOutput
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

TRANSPORTS = ("sse", "streamable-http", "stdio")
# Environment variable holding the server list as JSON (used by the Harbor runner).
MCP_SERVERS_ENV = "RHO_AGENT_MCP_SERVERS"
# How long to keep trying to reach a server at startup. A server in a sibling
# container may still be starting when the agent begins.
DEFAULT_CONNECT_TIMEOUT = 60.0
# Longest a single tool call may take.
DEFAULT_CALL_TIMEOUT = 600.0
# Idle time allowed on a server's event stream. Long, since the agent may think
# for many minutes between calls; a hung call is bounded by the call timeout.
STREAM_READ_TIMEOUT = 6 * 3600.0
_RETRY_DELAY = 2.0
# OpenAI function names: ^[a-zA-Z0-9_-]{1,64}$
_MAX_TOOL_NAME = 64
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPUnavailableError(RuntimeError):
    """The ``mcp`` package is not installed."""


@dataclass(frozen=True)
class MCPServerConfig:
    """One MCP server to connect to."""

    name: str
    transport: str = "sse"
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    # HTTP transports only. Values may reference ``${VAR}``.
    headers: dict[str, str] = field(default_factory=dict)
    # stdio only: extra environment for the server process.
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MCPServerConfig:
        if not isinstance(data, Mapping):
            raise ValueError(f"MCP server entry must be a mapping, got {type(data).__name__}")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("MCP server entry needs a 'name'")
        transport = str(data.get("transport") or "sse")
        if transport == "http":
            transport = "streamable-http"
        if transport not in TRANSPORTS:
            raise ValueError(
                f"MCP server '{name}': unknown transport {transport!r} "
                f"(expected one of {', '.join(TRANSPORTS)})"
            )
        url = data.get("url") or None
        command = data.get("command") or None
        if transport != "stdio" and not url:
            raise ValueError(f"MCP server '{name}': 'url' is required for {transport}")
        if transport == "stdio" and not command:
            raise ValueError(f"MCP server '{name}': 'command' is required for stdio")
        return cls(
            name=name,
            transport=transport,
            url=str(url) if url else None,
            command=str(command) if command else None,
            args=[str(a) for a in data.get("args") or []],
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "transport": self.transport}
        if self.url:
            data["url"] = self.url
        if self.command:
            data["command"] = self.command
        if self.args:
            data["args"] = list(self.args)
        if self.headers:
            data["headers"] = dict(self.headers)
        if self.env:
            data["env"] = dict(self.env)
        return data


def parse_mcp_servers(
    value: str | Iterable[Mapping[str, Any] | MCPServerConfig] | None,
) -> list[MCPServerConfig]:
    """Server configs from a JSON string, a list of mappings, or None.

    Raises:
        ValueError: malformed JSON, an invalid entry, or two servers with one name.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP server list is not valid JSON: {e}") from e
        if not isinstance(value, list):
            raise ValueError("MCP server list must be a JSON array")
    servers = [
        item if isinstance(item, MCPServerConfig) else MCPServerConfig.from_dict(item)
        for item in value
    ]
    names = [s.name for s in servers]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"Duplicate MCP server names: {', '.join(duplicates)}")
    return servers


def tool_name(server: str, tool: str) -> str:
    """The registry name of a server's tool: ``mcp__<server>__<tool>``, made API-safe."""
    name = _INVALID_NAME_CHARS.sub("_", f"mcp__{server}__{tool}")
    if len(name) > _MAX_TOOL_NAME:
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        name = f"{name[: _MAX_TOOL_NAME - 9]}_{digest}"
    return name


def _expand_env(value: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in env:
            raise ValueError(f"environment variable {var} is not set")
        return env[var]

    return _ENV_REF.sub(replace, value)


def _import_mcp() -> Any:
    try:
        import mcp
    except ImportError as e:
        raise MCPUnavailableError(
            "MCP servers need the 'mcp' package: uv pip install -e '.[mcp]'"
        ) from e
    return mcp


def _describe_error(error: BaseException) -> str:
    """One line for an error, unwrapping anyio's exception groups."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _server_errlog() -> Any:
    """Where a stdio server's stderr goes: our stderr when it is a real file."""
    for stream in (sys.stderr, sys.__stderr__):
        try:
            if stream is not None:
                stream.fileno()
                return stream
        except (AttributeError, OSError, ValueError):
            continue
    return subprocess.DEVNULL


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s must be a number of seconds, got %r; using %s", name, raw, default)
        return default


class MCPServerConnection:
    """A live connection to one MCP server.

    The transport and client session are entered and exited inside one
    background task, as anyio requires, so a server that fails or drops does
    not take the caller's task with it.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self._env = env
        self.tools: list[Any] = []
        self._session: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._session is not None and self._task is not None and not self._task.done()

    def _transport(self) -> contextlib.AbstractAsyncContextManager[Any]:
        """The SDK transport's context manager; it yields (read, write, ...)."""
        config = self.config
        if config.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            env = None
            if config.env:
                from mcp.client.stdio import get_default_environment

                env = {**get_default_environment(), **config.env}
            params = StdioServerParameters(command=config.command, args=config.args, env=env)
            return stdio_client(params, errlog=_server_errlog())

        source = os.environ if self._env is None else self._env
        headers = {k: _expand_env(v, source) for k, v in config.headers.items()} or None
        if config.transport == "sse":
            from mcp.client.sse import sse_client

            return sse_client(config.url, headers=headers, sse_read_timeout=STREAM_READ_TIMEOUT)

        try:
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:  # mcp < 1.24
            from mcp.client.streamable_http import streamablehttp_client

            return streamablehttp_client(
                config.url, headers=headers, sse_read_timeout=STREAM_READ_TIMEOUT
            )

        import httpx
        from mcp.shared._httpx_utils import create_mcp_http_client

        @contextlib.asynccontextmanager
        async def transport():
            client = create_mcp_http_client(
                headers=headers, timeout=httpx.Timeout(30.0, read=STREAM_READ_TIMEOUT)
            )
            async with client, streamable_http_client(config.url, http_client=client) as streams:
                yield streams

        return transport()

    async def _serve(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        from mcp import ClientSession

        try:
            async with contextlib.AsyncExitStack() as stack:
                streams = await stack.enter_async_context(self._transport())
                session = await stack.enter_async_context(
                    ClientSession(
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(seconds=self.call_timeout),
                    )
                )
                await session.initialize()
                tools: list[Any] = []
                cursor = None
                while True:
                    page = (
                        await session.list_tools(cursor) if cursor else await session.list_tools()
                    )
                    tools.extend(page.tools)
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                self.tools = tools
                self._session = session
                ready.set_result(None)
                await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.warning(
                    "MCP server '%s' disconnected: %s", self.config.name, _describe_error(e)
                )
        finally:
            self._session = None

    async def _connect_once(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        stop = asyncio.Event()
        task = asyncio.create_task(self._serve(ready, stop), name=f"mcp:{self.config.name}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout)
        except BaseException:
            stop.set()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            if not ready.done():
                ready.cancel()
            elif not ready.cancelled():
                ready.exception()  # mark retrieved
            raise
        self._task, self._stop = task, stop

    async def connect(self) -> None:
        """Connect and list tools, retrying until ``connect_timeout`` runs out.

        Raises the last connection error when every attempt failed.
        """
        _import_mcp()
        await self.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        while True:
            remaining = deadline - loop.time()
            try:
                await self._connect_once(max(remaining, 1.0))
                return
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(e, ValueError) or deadline - loop.time() <= _RETRY_DELAY:
                    raise  # a bad config does not get better with retries
                logger.info(
                    "MCP server '%s' not reachable yet (%s); retrying",
                    self.config.name,
                    _describe_error(e),
                )
                await asyncio.sleep(_RETRY_DELAY)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one of the server's tools, reconnecting first if the connection dropped."""
        async with self._lock:
            if not self.connected:
                logger.warning("MCP server '%s' is not connected; reconnecting", self.config.name)
                await self.connect()
            session = self._session
        assert session is not None
        return await session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=self.call_timeout)
        )

    async def close(self) -> None:
        task, stop = self._task, self._stop
        self._task = self._stop = None
        self._session = None
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(task, 10.0)
        except BaseException:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    schema = dict(schema) if isinstance(schema, Mapping) else {}
    schema["type"] = "object"
    schema.setdefault("properties", {})
    return schema


class MCPToolHandler(ToolHandler):
    """One tool of an MCP server, exposed as ``mcp__<server>__<tool>``."""

    def __init__(
        self, connection: MCPServerConnection, tool: Any, *, attach_images: bool = True
    ) -> None:
        self._connection = connection
        self._tool = tool
        self._name = tool_name(connection.config.name, tool.name)
        self._schema = _input_schema(tool)
        self._attach_images = attach_images

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_name(self) -> str:
        return self._connection.config.name

    @property
    def tool_name(self) -> str:
        """The tool's own name on its server."""
        return str(self._tool.name)

    @property
    def description(self) -> str:
        text = (self._tool.description or getattr(self._tool, "title", None) or "").strip()
        prefix = f"[MCP server '{self.server_name}']"
        return f"{prefix} {text}" if text else prefix

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        try:
            result = await self._connection.call_tool(self.tool_name, invocation.arguments)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ToolOutput(
                content=(
                    f"MCP tool {self.tool_name} on server '{self.server_name}' failed: "
                    f"{_describe_error(e)}"
                ),
                success=False,
            )
        return result_to_output(result, label=self._name, attach_images=self._attach_images)


def _image(data: str, media_type: str, label: str) -> tuple[dict[str, str] | None, str]:
    """An image attachment for the session, or None and a reason it was left out."""
    from .handlers.view_image import MAX_IMAGE_BYTES, _shrink

    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None, "invalid base64"
    if len(raw) > MAX_IMAGE_BYTES:
        shrunk = _shrink(raw)
        if shrunk is None:
            return None, f"{len(raw) / 1e6:.1f} MB, too large to attach"
        raw, media_type, _ = shrunk
        data = base64.b64encode(raw).decode("ascii")
    return {"path": label, "media_type": media_type, "data": data}, ""


def result_to_output(result: Any, *, label: str, attach_images: bool = True) -> ToolOutput:
    """Convert an MCP ``CallToolResult`` into a ToolOutput.

    Text is joined; images go on ``ToolOutput.images`` (the session shows them
    to the model) or become a placeholder when images are off; other content
    becomes a short placeholder.
    """
    parts: list[str] = []
    images: list[dict[str, str]] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", None)
        if kind == "text":
            parts.append(item.text)
        elif kind == "image":
            media_type = getattr(item, "mimeType", None) or "image/png"
            if not attach_images:
                parts.append(f"[image ({media_type}) omitted: image viewing is off]")
                continue
            image, reason = _image(item.data, media_type, f"{label} image {len(images) + 1}")
            if image is None:
                parts.append(f"[image ({media_type}) omitted: {reason}]")
            else:
                images.append(image)
                parts.append(f"[image {len(images)} attached; it follows in the next message]")
        elif kind == "audio":
            parts.append(f"[audio ({getattr(item, 'mimeType', '?')}) omitted]")
        elif kind == "resource":
            resource = item.resource
            text = getattr(resource, "text", None)
            if text is not None:
                parts.append(f"[resource {resource.uri}]\n{text}")
            else:
                mime = getattr(resource, "mimeType", None) or "binary"
                parts.append(f"[resource {resource.uri} ({mime}) omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {item.uri}]")
        elif kind is not None:
            parts.append(f"[{kind} content omitted]")

    structured = getattr(result, "structuredContent", None)
    if structured is not None and not any(p.strip() for p in parts):
        parts.append(json.dumps(structured, indent=2, default=str))

    is_error = bool(getattr(result, "isError", False))
    content = "\n".join(parts).strip() or ("(error with no message)" if is_error else "(no output)")
    return ToolOutput(
        content=content,
        success=not is_error,
        metadata={"mcp": True},
        images=images,
    )


@dataclass
class MCPServers:
    """The MCP servers of one agent: connects them and registers their tools."""

    configs: list[MCPServerConfig]
    attach_images: bool = True
    connect_timeout: float = field(
        default_factory=lambda: _env_seconds(
            "RHO_AGENT_MCP_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT
        )
    )
    call_timeout: float = field(
        default_factory=lambda: _env_seconds("RHO_AGENT_MCP_CALL_TIMEOUT", DEFAULT_CALL_TIMEOUT)
    )
    connections: dict[str, MCPServerConnection] = field(default_factory=dict)
    # Server name -> error, for servers that could not be reached.
    failed: dict[str, str] = field(default_factory=dict)
    registered: list[str] = field(default_factory=list)

    async def connect(self, registry: ToolRegistry) -> None:
        """Connect every server concurrently and register the tools of those that answer.

        Never raises for an unreachable server, or when the ``mcp`` package is
        missing: the server is logged and recorded in ``failed``.
        """
        if not self.configs:
            return
        try:
            _import_mcp()
        except MCPUnavailableError as e:
            for config in self.configs:
                self.failed[config.name] = str(e)
            logger.warning("MCP servers skipped: %s", e)
            return
        connections = [
            MCPServerConnection(
                config, connect_timeout=self.connect_timeout, call_timeout=self.call_timeout
            )
            for config in self.configs
        ]
        results = await asyncio.gather(*(c.connect() for c in connections), return_exceptions=True)
        for connection, result in zip(connections, results, strict=True):
            name = connection.config.name
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                self.failed[name] = _describe_error(result)
                logger.warning(
                    "MCP server '%s' could not be reached and is skipped: %s",
                    name,
                    self.failed[name],
                )
                continue
            self.connections[name] = connection
            for tool in connection.tools:
                handler = MCPToolHandler(connection, tool, attach_images=self.attach_images)
                if handler.name in registry:
                    logger.warning(
                        "MCP tool %s from server '%s' clashes with an existing tool; skipped",
                        handler.name,
                        name,
                    )
                    continue
                registry.register(handler)
                self.registered.append(handler.name)

    async def close(self, registry: ToolRegistry | None = None) -> None:
        """Disconnect every server and, given a registry, remove their tools."""
        connections, self.connections = self.connections, {}
        await asyncio.gather(*(c.close() for c in connections.values()), return_exceptions=True)
        if registry is not None:
            for name in self.registered:
                registry.unregister(name)
        self.registered = []
        self.failed = {}
