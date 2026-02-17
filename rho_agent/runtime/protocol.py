"""Runtime protocol — the behavioral contract every runtime backend must satisfy."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.agent import Agent
from ..core.session import Session
from ..observability.processor import ObservabilityProcessor
from ..tools.registry import ToolRegistry
from .options import RuntimeOptions
from .types import ApprovalCallback

if TYPE_CHECKING:
    from .types import RunState, ToolApprovalItem


@runtime_checkable
class Runtime(Protocol):
    """Structural interface for runtime backends.

    Read-only properties expose the runtime's collaborators.
    ``start`` / ``close`` bracket the session lifecycle.
    ``restore_state`` / ``capture_state`` support interrupt/resume.
    """

    @property
    def agent(self) -> Agent: ...

    @property
    def session(self) -> Session: ...

    @property
    def registry(self) -> ToolRegistry: ...

    @property
    def model(self) -> str: ...

    @property
    def profile_name(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def options(self) -> RuntimeOptions: ...

    @property
    def approval_callback(self) -> ApprovalCallback | None: ...

    @property
    def cancel_check(self) -> Callable[[], bool] | None: ...

    @property
    def observability(self) -> ObservabilityProcessor | None: ...

    async def start(self) -> None: ...

    async def close(self, status: str = "completed") -> None: ...

    def restore_state(self, state: RunState) -> None: ...

    def capture_state(self, interruptions: list[ToolApprovalItem]) -> RunState: ...
