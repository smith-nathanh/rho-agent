"""Wrap a pre-configured agent as a callable tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from typing import Any

from ...core.session import Session
from ...runtime.options import RuntimeOptions
from ...runtime.types import ApprovalCallback
from ..base import ToolHandler, ToolInvocation, ToolOutput


class AgentToolHandler(ToolHandler):
    """A tool backed by an independent agent with its own identity.

    Unlike ``DelegateHandler`` which derives its identity from the parent,
    ``AgentToolHandler`` has a fixed system prompt, profile, and model
    configured at construction time.  Each invocation spawns an independent
    runtime — no conversation history is shared with the caller.

    Example::

        sql_agent = AgentToolHandler(
            tool_name="generate_sql",
            tool_description="Generate SQL from a natural language question.",
            system_prompt="You are an expert SQL developer. ...",
            options=RuntimeOptions(model="gpt-5-mini", profile="readonly"),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question",
                    },
                },
                "required": ["question"],
            },
        )
        registry.register(sql_agent)
    """

    def __init__(
        self,
        *,
        tool_name: str,
        tool_description: str,
        system_prompt: str,
        options: RuntimeOptions | None = None,
        input_schema: dict[str, Any] | None = None,
        input_formatter: Callable[[dict[str, Any]], str] | None = None,
        approval_callback: ApprovalCallback | None = None,
        cancel_check: Callable[[], bool] | None = None,
        requires_approval: bool = False,
    ) -> None:
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._system_prompt = system_prompt
        self._options = options or RuntimeOptions()
        self._input_schema = input_schema or {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Instruction for the agent.",
                },
            },
            "required": ["instruction"],
        }
        self._input_formatter = input_formatter or _default_formatter
        self._approval_callback = approval_callback
        self._cancel_check = cancel_check
        self._requires_approval = requires_approval

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._tool_description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._input_schema

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        instruction = self._input_formatter(invocation.arguments)
        if not instruction.strip():
            return ToolOutput(
                content=f"{self._tool_name}: received empty instruction.",
                success=False,
            )

        # Disable delegation in the child to prevent unbounded recursion.
        child_options = replace(self._options, enable_delegate=False, session_id=None)
        child_session = Session(system_prompt=self._system_prompt)

        from ...runtime.factory import create_runtime
        from ...runtime.run import run_prompt

        child_runtime = create_runtime(
            self._system_prompt,
            options=child_options,
            session=child_session,
            approval_callback=self._approval_callback,
            cancel_check=self._cancel_check,
        )

        started = monotonic()
        try:
            async with child_runtime:
                result = await run_prompt(child_runtime, instruction)
                child_runtime.close_status = result.status
                return ToolOutput(
                    content=result.text,
                    success=result.status == "completed",
                    metadata={
                        "child_usage": result.usage,
                        "child_status": result.status,
                        "child_session_id": child_runtime.session_id,
                        "duration_seconds": round(monotonic() - started, 2),
                    },
                )
        except Exception as exc:
            return ToolOutput(
                content=f"{self._tool_name} failed: {type(exc).__name__}: {exc}",
                success=False,
                metadata={
                    "child_status": "error",
                    "duration_seconds": round(monotonic() - started, 2),
                },
            )


def _default_formatter(arguments: dict[str, Any]) -> str:
    """Format typed arguments into an instruction string.

    If the schema has a single ``instruction`` field, return it directly.
    Otherwise format each key-value pair on its own line.
    """
    if list(arguments.keys()) == ["instruction"]:
        return str(arguments["instruction"])
    parts: list[str] = []
    for key, value in arguments.items():
        if isinstance(value, str):
            parts.append(f"{key}: {value}")
        else:
            parts.append(f"{key}: {json.dumps(value)}")
    return "\n".join(parts)
