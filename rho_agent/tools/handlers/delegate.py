"""Delegate work to a single child agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import os
from time import monotonic
from typing import Any

from ...core.session import Session
from ...runtime.options import RuntimeOptions
from ...runtime.types import ApprovalCallback
from ...signals import AgentInfo, SignalManager
from ..base import ToolHandler, ToolInvocation, ToolOutput


class DelegateHandler(ToolHandler):
    """Spawn one child runtime to execute a focused instruction."""

    def __init__(
        self,
        *,
        parent_session: Session,
        parent_options: RuntimeOptions,
        parent_approval_callback: ApprovalCallback | None,
        parent_cancel_check: Callable[[], bool] | None,
        parent_agent_cancel_check: Callable[[], bool] | None,
        requires_approval: bool,
    ) -> None:
        self._parent_session = parent_session
        self._parent_options = parent_options
        self._parent_approval_callback = parent_approval_callback
        self._parent_cancel_check = parent_cancel_check
        self._parent_agent_cancel_check = parent_agent_cancel_check
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
        instruction = str(invocation.arguments.get("instruction", "")).strip()
        full_context = bool(invocation.arguments.get("full_context", False))

        if not instruction:
            return ToolOutput(content="Delegate requires a non-empty instruction.", success=False)

        child_session = Session(system_prompt=self._parent_session.system_prompt)
        if full_context:
            child_session.history = deepcopy(self._parent_session.history)

        parent_session_id = self._parent_options.session_id
        telemetry_metadata = dict(self._parent_options.telemetry_metadata)
        telemetry_metadata["parent_session_id"] = parent_session_id
        child_options = replace(
            self._parent_options,
            enable_delegate=False,
            session_id=None,
            telemetry_metadata=telemetry_metadata,
        )

        # Local import to avoid runtime import cycles during bootstrap.
        from ...runtime.factory import create_runtime
        from ...runtime.lifecycle import close_runtime, start_runtime
        from ...runtime.run import run_prompt

        signal_manager = SignalManager()
        child_session_id_ref: list[str | None] = [None]

        def _child_cancel_check() -> bool:
            if self._parent_agent_cancel_check is not None and self._parent_agent_cancel_check():
                return True
            if self._parent_cancel_check is not None and self._parent_cancel_check():
                return True
            session_id = child_session_id_ref[0]
            if session_id and signal_manager.is_cancelled(session_id):
                return True
            return False

        child_cancel_check: Callable[[], bool] | None = _child_cancel_check

        child_runtime = create_runtime(
            child_session.system_prompt,
            options=child_options,
            session=child_session,
            approval_callback=self._parent_approval_callback,
            cancel_check=child_cancel_check,
        )
        child_session_id = getattr(child_runtime, "session_id", None)
        child_session_id_ref[0] = child_session_id
        child_registered = False
        if isinstance(child_session_id, str) and child_session_id:
            signal_manager.register(
                AgentInfo(
                    session_id=child_session_id,
                    pid=os.getpid(),
                    model=child_options.model,
                    instruction_preview=instruction[:100],
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            child_registered = True

        status = "error"
        started = monotonic()
        try:
            if child_registered:
                # Approval has already happened at this point; child execution starts now.
                print(f"[delegate] Sub-agent {child_session_id[:8]} started", flush=True)
            await start_runtime(child_runtime)
            result = await run_prompt(child_runtime, instruction)
            status = result.status
            return ToolOutput(
                content=result.text,
                success=result.status == "completed",
                metadata={
                    "child_usage": result.usage,
                    "child_status": result.status,
                    "child_session_id": child_session_id,
                    "duration_seconds": round(monotonic() - started, 2),
                },
            )
        except Exception as exc:
            return ToolOutput(
                content=f"Delegate child failed: {type(exc).__name__}: {exc}",
                success=False,
                metadata={
                    "child_status": "error",
                    "child_session_id": child_session_id,
                    "duration_seconds": round(monotonic() - started, 2),
                },
            )
        finally:
            await close_runtime(child_runtime, status=status)
            if child_registered:
                signal_manager.deregister(child_session_id)
