"""Delegate work to a single child agent."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import os
from time import monotonic
from typing import Any

from ...core.config import AgentConfig
from ...core.events import ApprovalCallback
from ...core.state import State
from ..base import ToolHandler, ToolInvocation, ToolOutput


class DelegateHandler(ToolHandler):
    """Spawn one child agent to execute a focused instruction."""

    def __init__(
        self,
        *,
        parent_config: AgentConfig,
        parent_system_prompt: str,
        parent_state: State,
        parent_approval_callback: ApprovalCallback | None,
        parent_cancel_check: Callable[[], bool] | None,
        requires_approval: bool,
    ) -> None:
        self._parent_config = parent_config
        self._parent_system_prompt = parent_system_prompt
        self._parent_state = parent_state
        self._parent_approval_callback = parent_approval_callback
        self._parent_cancel_check = parent_cancel_check
        self._requires_approval = requires_approval

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        return (
            "Spawn a one-time child agent to execute a focused instruction and return "
            "its final text output."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Instruction for the child agent to execute.",
                },
                "full_context": {
                    "type": "boolean",
                    "description": (
                        "If true, child receives a snapshot of parent conversation history. "
                        "If false, child starts with empty history."
                    ),
                    "default": False,
                },
            },
            "required": ["instruction"],
        }

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Spawn a child agent and return its final output."""
        instruction = str(invocation.arguments.get("instruction", "")).strip()
        full_context = bool(invocation.arguments.get("full_context", False))

        if not instruction:
            return ToolOutput(content="Delegate requires a non-empty instruction.", success=False)

        # Lazy import to avoid circular imports
        from ...core.agent import Agent
        from ...core.session import Session

        # Build child state, optionally with parent history
        child_state = State()
        if full_context:
            child_state.messages = deepcopy(self._parent_state.messages)

        # Build child agent (no delegate to prevent recursion)
        child_config = AgentConfig(
            system_prompt=self._parent_system_prompt,
            model=self._parent_config.model,
            base_url=self._parent_config.base_url,
            service_tier=self._parent_config.service_tier,
            reasoning_effort=self._parent_config.reasoning_effort,
            profile=self._parent_config.profile,
            working_dir=self._parent_config.working_dir,
            auto_approve=self._parent_config.auto_approve,
        )
        child_agent = Agent(child_config)
        # Remove delegate tool from child to prevent recursion
        if "delegate" in child_agent.registry:
            child_agent.registry.unregister("delegate")

        child_session = Session(child_agent, state=child_state)
        child_session.approval_callback = self._parent_approval_callback
        child_session.cancel_check = self._parent_cancel_check

        started = monotonic()
        try:
            print(f"[delegate] Sub-agent {child_session.id[:8]} started", flush=True)
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
                content=f"Delegate child failed: {type(exc).__name__}: {exc}",
                success=False,
                metadata={
                    "child_status": "error",
                    "child_session_id": child_session.id,
                    "duration_seconds": round(monotonic() - started, 2),
                },
            )
