"""Wrap a pre-configured agent as a callable tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from time import monotonic
from typing import Any

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.events import ApprovalCallback
from ...core.session import Session
from ..base import ToolHandler, ToolInvocation, ToolOutput


class AgentToolHandler(ToolHandler):
    """A tool backed by an independent agent with its own identity.

    Unlike ``DelegateHandler`` which derives its identity from the parent,
    ``AgentToolHandler`` has a fixed system prompt, profile, and model
    configured at construction time.  Each invocation spawns an independent
    session — no conversation history is shared with the caller.

    Example::

        sql_agent = AgentToolHandler(
            tool_name="generate_sql",
            tool_description="Generate SQL from a natural language question.",
            system_prompt="You are an expert SQL developer. ...",
            config=AgentConfig(model="gpt-5-mini", profile="readonly"),
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
        config: AgentConfig | None = None,
        input_schema: dict[str, Any] | None = None,
        input_formatter: Callable[[dict[str, Any]], str] | None = None,
        approval_callback: ApprovalCallback | None = None,
        cancel_check: Callable[[], bool] | None = None,
        requires_approval: bool = False,
    ) -> None:
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._system_prompt = system_prompt
        self._config = config or AgentConfig()
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
        """Spawn a child agent to execute the instruction."""
        instruction = self._input_formatter(invocation.arguments)
        if not instruction.strip():
            return ToolOutput(
                content=f"{self._tool_name}: received empty instruction.",
                success=False,
            )

        # Build child with its own system prompt, disabling delegate to prevent recursion
        child_config = AgentConfig(
            system_prompt=self._system_prompt,
            model=self._config.model,
            base_url=self._config.base_url,
            service_tier=self._config.service_tier,
            reasoning_effort=self._config.reasoning_effort,
            profile=self._config.profile,
            working_dir=self._config.working_dir,
            auto_approve=self._config.auto_approve,
        )
        child_agent = Agent(child_config)
        if "delegate" in child_agent.registry:
            child_agent.registry.unregister("delegate")

        child_session = Session(child_agent)
        child_session.approval_callback = self._approval_callback
        child_session.cancel_check = self._cancel_check

        started = monotonic()
        try:
            async with child_session:
                result = await child_session.run(instruction)
                return ToolOutput(
                    content=result.text,
                    success=result.status == "completed",
                    metadata={
                        "child_usage": result.usage,
                        "child_status": result.status,
                        "child_session_id": child_session.id,
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
