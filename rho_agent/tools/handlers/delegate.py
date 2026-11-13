"""Delegate work to a single child agent."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from time import monotonic
from typing import Any

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.events import ApprovalCallback
from ...core.session import Session
from ...core.state import State
from ..base import ToolHandler, ToolInvocation, ToolOutput
from ._child import child_session_kwargs, link_child, settle_child, share_sandbox


class DelegateHandler(ToolHandler):
    """Spawn one child agent to execute a focused instruction.

    The child takes its identity (model, profile, system prompt, working
    directory) from the session that calls the tool, so one handler instance
    can sit in a registry shared by many sessions. The ``parent_*`` arguments
    are only a fallback for calls made outside a session.

    The child is linked to its parent: it shares the model client, writes its
    trace under the parent's session directory, is capped by the parent's
    remaining cost and time budget, and its usage is added to the parent's.
    Several delegate calls in one model turn run concurrently.
    """

    def __init__(
        self,
        *,
        parent_config: AgentConfig | None = None,
        parent_system_prompt: str | None = None,
        parent_state: State | None = None,
        parent_approval_callback: ApprovalCallback | None = None,
        parent_cancel_check: Callable[[], bool] | None = None,
        requires_approval: bool = False,
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
            "its final text output. Use it only when the user asks for subagents or "
            "parallel work, or when a larger job has two or more independent pieces "
            "to run at the same time. Never delegate a single piece of work, and never "
            "delegate investigating or checking the task you were given: do that "
            "yourself, which is quicker and keeps the findings in your own context. "
            "The child has the same tools as you but none of your conversation unless "
            "full_context is set, so give it a self-contained instruction. Several "
            "delegate calls in one turn run in parallel."
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

    @property
    def parallel_safe(self) -> bool:
        return True

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Spawn a child agent and return its final output."""
        instruction = str(invocation.arguments.get("instruction", "")).strip()
        full_context = bool(invocation.arguments.get("full_context", False))

        if not instruction:
            return ToolOutput(content="Delegate requires a non-empty instruction.", success=False)

        parent = invocation.session
        if parent is not None:
            parent_config = parent.agent.config
            parent_system_prompt = parent.agent.system_prompt
            parent_state = parent.state
        else:
            parent_config = self._parent_config
            parent_system_prompt = self._parent_system_prompt
            parent_state = self._parent_state
        if parent_config is None:
            return ToolOutput(
                content="Delegate is not available: no parent session or configuration.",
                success=False,
            )

        # Build child state, optionally with parent history
        child_state = State()
        if full_context and parent_state is not None:
            messages = deepcopy(parent_state.messages)
            # The parent's history ends with the assistant message that issued
            # this very call, whose tool calls have no results yet.
            child_state.messages = messages
            child_state.close_pending_tool_calls("(this call is the one you are executing)")

        # The child inherits everything except the caps (it gets the parent's
        # remaining budget instead) and the already-resolved system prompt.
        child_config = replace(
            parent_config,
            system_prompt=parent_system_prompt or "",
            max_cost_usd=None,
            max_wall_seconds=None,
        )
        child_agent = Agent(child_config)
        if parent_system_prompt is not None:
            child_agent._system_prompt = parent_system_prompt
        # Remove delegate tool from child to prevent recursion
        if "delegate" in child_agent.registry:
            child_agent.registry.unregister("delegate")
        share_sandbox(parent, child_agent)

        child_session = Session(
            child_agent, **child_session_kwargs(parent, child_state, share_client=True)
        )
        if parent is None:
            child_session.approval_callback = self._parent_approval_callback
            child_session.cancel_check = self._parent_cancel_check
        link_child(parent, child_session, self.name, instruction)

        started = monotonic()
        status = "error"
        try:
            async with child_session:
                result = await child_session.run(instruction)
                status = result.status
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
        finally:
            settle_child(parent, child_session, status)
