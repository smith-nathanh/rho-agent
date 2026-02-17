"""Daytona cloud sandbox runtime — tools execute in a remote VM."""

from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..core.agent import Agent
from ..core.session import Session
from ..observability.processor import ObservabilityProcessor
from ..tools.registry import ToolRegistry
from .options import RuntimeOptions
from .types import ApprovalCallback, RunState, ToolApprovalItem


@dataclass
class DaytonaRuntime:
    """Runtime that executes tools in a Daytona cloud sandbox.

    Owns the :class:`SandboxManager` lifecycle — the sandbox is created
    lazily on the first tool call and torn down on :meth:`close`.
    """

    agent: Agent
    session: Session
    registry: ToolRegistry
    model: str
    profile_name: str
    session_id: str
    options: RuntimeOptions
    approval_callback: ApprovalCallback | None = None
    cancel_check: Callable[[], bool] | None = None
    observability: ObservabilityProcessor | None = None

    # Daytona-specific — set during construction, not by callers.
    _manager: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Runtime protocol
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start observability session (sandbox is created lazily)."""
        if self.observability:
            await self.observability.start_session()

    async def close(self, status: str = "completed") -> None:
        """Delete the remote sandbox and end observability."""
        if self._manager is not None:
            await self._manager.close()
            self._manager = None

        if self.observability:
            await self.observability.end_session(status)

    def restore_state(self, state: RunState) -> None:
        """Mutate runtime in-place from a serialized run snapshot."""
        self.session_id = state.session_id
        self.options.session_id = state.session_id
        if self.observability:
            self.observability.context.session_id = state.session_id
        self.session.system_prompt = state.system_prompt
        self.session.history = deepcopy(state.history)
        self.session.total_input_tokens = state.total_input_tokens
        self.session.total_output_tokens = state.total_output_tokens
        self.session.total_cached_tokens = state.total_cached_tokens
        self.session.total_reasoning_tokens = state.total_reasoning_tokens
        self.session.total_cost_usd = state.total_cost_usd
        self.session.last_input_tokens = state.last_input_tokens

    def capture_state(self, interruptions: list[ToolApprovalItem]) -> RunState:
        """Build a serializable run snapshot from the current runtime session."""
        return RunState(
            session_id=self.session_id,
            system_prompt=self.session.system_prompt,
            history=deepcopy(self.session.history),
            total_input_tokens=self.session.total_input_tokens,
            total_output_tokens=self.session.total_output_tokens,
            total_cached_tokens=self.session.total_cached_tokens,
            total_reasoning_tokens=self.session.total_reasoning_tokens,
            total_cost_usd=self.session.total_cost_usd,
            last_input_tokens=self.session.last_input_tokens,
            pending_approvals=interruptions,
        )

    # ------------------------------------------------------------------
    # Construction helper
    # ------------------------------------------------------------------

    @staticmethod
    def register_daytona_tools(
        registry: ToolRegistry,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> Any:
        """Register Daytona remote handlers and return the SandboxManager.

        This replaces the old ``ToolFactory._register_daytona_tools`` method,
        keeping all Daytona wiring in one place.

        Returns:
            The :class:`SandboxManager` that backs the registered handlers.

        Raises:
            ImportError: If the ``daytona`` SDK is not installed.
        """
        try:
            from ..tools.handlers.daytona import (
                DaytonaBashHandler,
                DaytonaEditHandler,
                DaytonaGlobHandler,
                DaytonaGrepHandler,
                DaytonaListHandler,
                DaytonaReadHandler,
                DaytonaWriteHandler,
                SandboxManager,
            )
        except ImportError as exc:
            raise ImportError(
                "Daytona SDK not installed. Install with: uv pip install 'rho-agent[daytona]'"
            ) from exc

        resolved_env = env if env is not None else dict(os.environ)
        manager = SandboxManager.from_env(working_dir=working_dir, env=resolved_env)

        registry.register(DaytonaBashHandler(manager))
        registry.register(DaytonaReadHandler(manager))
        registry.register(DaytonaWriteHandler(manager))
        registry.register(DaytonaEditHandler(manager))
        registry.register(DaytonaGlobHandler(manager))
        registry.register(DaytonaGrepHandler(manager))
        registry.register(DaytonaListHandler(manager))

        return manager
