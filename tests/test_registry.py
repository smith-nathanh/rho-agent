"""Tests for ToolRegistry dispatch, coercion, and error handling."""

from __future__ import annotations

from typing import Any

import pytest

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry, _coerce_arguments


# --- Inline stub handler ---


class StubHandler(ToolHandler):
    """Minimal handler for testing registry dispatch."""

    def __init__(
        self,
        name: str = "stub",
        *,
        enabled: bool = True,
        approval: bool = False,
        response: str = "ok",
        raise_on_handle: Exception | None = None,
    ):
        self._name = name
        self._enabled = enabled
        self._approval = approval
        self._response = response
        self._raise_on_handle = raise_on_handle

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Stub: {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def requires_approval(self) -> bool:
        return self._approval

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        if self._raise_on_handle:
            raise self._raise_on_handle
        return ToolOutput(content=self._response)


def _invoke(name: str, **kwargs: Any) -> ToolInvocation:
    return ToolInvocation(call_id="test-id", tool_name=name, arguments=kwargs)


# --- Dispatch tests ---


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_handler():
    reg = ToolRegistry()
    reg.register(StubHandler("alpha", response="from-alpha"))
    reg.register(StubHandler("beta", response="from-beta"))

    result = await reg.dispatch(_invoke("beta"))
    assert result.content == "from-beta"
    assert result.success is True


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error():
    reg = ToolRegistry()
    result = await reg.dispatch(_invoke("nonexistent"))
    assert result.success is False
    assert result.content  # non-empty error message


@pytest.mark.asyncio
async def test_dispatch_disabled_tool_returns_error():
    reg = ToolRegistry()
    reg.register(StubHandler("disabled_tool", enabled=False, response="should not see"))

    result = await reg.dispatch(_invoke("disabled_tool"))
    assert result.success is False
    assert "disabled_tool" in result.content


@pytest.mark.asyncio
async def test_dispatch_handler_exception_wrapped():
    reg = ToolRegistry()
    reg.register(StubHandler("boom", raise_on_handle=RuntimeError("kaboom")))

    result = await reg.dispatch(_invoke("boom"))
    assert result.success is False
    assert result.content  # non-empty


# --- Coercion tests ---


def test_coerce_string_true_to_bool():
    schema = {"properties": {"flag": {"type": "boolean"}}}
    result = _coerce_arguments({"flag": "true"}, schema)
    assert result["flag"] is True


def test_coerce_string_int():
    schema = {"properties": {"count": {"type": "integer"}}}
    result = _coerce_arguments({"count": "5"}, schema)
    assert result["count"] == 5
    assert isinstance(result["count"], int)


def test_coerce_leaves_correct_types_alone():
    schema = {"properties": {"flag": {"type": "boolean"}, "count": {"type": "integer"}}}
    result = _coerce_arguments({"flag": True, "count": 5}, schema)
    assert result["flag"] is True
    assert result["count"] == 5


# --- get_specs tests ---


def test_get_specs_excludes_disabled():
    reg = ToolRegistry()
    reg.register(StubHandler("visible", enabled=True))
    reg.register(StubHandler("hidden", enabled=False))

    specs = reg.get_specs()
    names = [s["function"]["name"] for s in specs]
    assert "visible" in names
    assert "hidden" not in names
