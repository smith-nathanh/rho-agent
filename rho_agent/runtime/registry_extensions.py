"""Runtime-level tool registration extensions."""

from __future__ import annotations

from collections.abc import Callable

from ..capabilities import CapabilityProfile
from ..core.session import Session
from ..tools.registry import ToolRegistry
from .options import RuntimeOptions
from .types import ApprovalCallback


def register_runtime_tools(
    registry: ToolRegistry,
    *,
    runtime_session: Session,
    runtime_options: RuntimeOptions,
    approval_callback: ApprovalCallback | None,
    cancel_check: Callable[[], bool] | None,
    parent_agent_cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Register tools that require runtime context rather than static profile config."""
    if not runtime_options.enable_delegate:
        return

    profile = runtime_options.profile
    if not isinstance(profile, CapabilityProfile):
        raise TypeError(
            "runtime_options.profile must be a CapabilityProfile before extension registration"
        )

    from ..tools.handlers.delegate import DelegateHandler

    registry.register(
        DelegateHandler(
            parent_session=runtime_session,
            parent_options=runtime_options,
            parent_approval_callback=approval_callback,
            parent_cancel_check=cancel_check,
            parent_agent_cancel_check=parent_agent_cancel_check,
            requires_approval=profile.requires_tool_approval("delegate"),
        )
    )
