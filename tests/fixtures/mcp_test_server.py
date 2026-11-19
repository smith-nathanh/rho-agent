"""A small MCP server for the MCP client tests.

Usage: python mcp_test_server.py [stdio | sse PORT | streamable-http PORT]
"""

from __future__ import annotations

import base64
import os
import sys

from mcp.server.fastmcp import Context, FastMCP, Image

# 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
server = FastMCP("test", host="127.0.0.1", port=port)


@server.tool(description="Add two integers.")
def add(a: int, b: int) -> int:
    return a + b


@server.tool(name="screen.shot", description="Return a tiny PNG.")
def screenshot() -> Image:
    return Image(data=PNG, format="png")


@server.tool(description="Always fails.")
def explode() -> str:
    raise RuntimeError("kaboom")


@server.tool(description="Return the server's process ID.")
def pid() -> int:
    return os.getpid()


@server.tool(description="Return the request's X-Test-Token header.")
def whoami(ctx: Context) -> str:
    request = ctx.request_context.request
    return request.headers.get("x-test-token", "<none>") if request is not None else "<stdio>"


if __name__ == "__main__":
    server.run(transport=transport)
