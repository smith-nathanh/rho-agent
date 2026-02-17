"""Runtime reconfiguration helpers."""

from __future__ import annotations

from ..capabilities import CapabilityProfile
from .builder import build_runtime_registry
from .factory import _auto_approve, _reject_all
from .types import ApprovalCallback, LocalRuntime


def _resolve_reconfigured_approval_callback(
    current_callback: ApprovalCallback | None,
    auto_approve: bool | None,
) -> ApprovalCallback | None:
    """Adjust default approval callback when auto_approve is reconfigured.

    Explicit callbacks are preserved. Only default callbacks created by
    create_runtime are switched when auto_approve changes.
    """
    if auto_approve is None:
        return current_callback

    if current_callback in (_auto_approve, _reject_all, None):
        return _auto_approve if auto_approve else _reject_all

    return current_callback


def reconfigure_runtime(
    runtime: LocalRuntime,
    *,
    profile: str | CapabilityProfile | None = None,
    working_dir: str | None = None,
    auto_approve: bool | None = None,
    enable_delegate: bool | None = None,
) -> CapabilityProfile:
    """Rebuild runtime registry/options via shared builder and swap active tool set."""
    parent_agent_cancel_check = getattr(runtime.agent, "is_cancelled", None)
    if not callable(parent_agent_cancel_check):
        parent_agent_cancel_check = None
    updated_approval_callback = _resolve_reconfigured_approval_callback(
        runtime.approval_callback,
        auto_approve,
    )

    build = build_runtime_registry(
        runtime_session=runtime.session,
        runtime_options=runtime.options,
        approval_callback=updated_approval_callback,
        cancel_check=runtime.cancel_check,
        parent_agent_cancel_check=parent_agent_cancel_check,
        profile=profile,
        working_dir=working_dir,
        auto_approve=auto_approve,
        enable_delegate=enable_delegate,
    )

    runtime.registry = build.registry
    runtime.agent.set_registry(build.registry)
    runtime.profile_name = build.capability_profile.name
    runtime.options = build.runtime_options
    runtime.approval_callback = updated_approval_callback
    set_approval_callback = getattr(runtime.agent, "set_approval_callback", None)
    if callable(set_approval_callback):
        set_approval_callback(updated_approval_callback)

    if runtime.observability and runtime.observability.context:
        runtime.observability.context.profile = build.capability_profile.name

    return build.capability_profile
