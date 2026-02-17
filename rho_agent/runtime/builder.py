"""Shared runtime builder helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from ..capabilities import CapabilityProfile
from ..capabilities.factory import ToolFactory, load_profile
from ..core.session import Session
from ..tools.registry import ToolRegistry
from .options import RuntimeOptions
from .registry_extensions import register_runtime_tools
from .types import ApprovalCallback


def resolve_profile(
    profile: str | CapabilityProfile | None,
) -> CapabilityProfile:
    """Resolve profile input to a concrete CapabilityProfile."""
    if isinstance(profile, CapabilityProfile):
        return profile
    if profile:
        return load_profile(profile)
    return CapabilityProfile.readonly()


@dataclass(frozen=True)
class RuntimeRegistryBuild:
    """Registry build result shared by create/reconfigure flows."""

    capability_profile: CapabilityProfile
    runtime_options: RuntimeOptions
    registry: ToolRegistry


def build_runtime_registry(
    *,
    runtime_session: Session,
    runtime_options: RuntimeOptions,
    approval_callback: ApprovalCallback | None,
    cancel_check: Callable[[], bool] | None,
    parent_agent_cancel_check: Callable[[], bool] | None,
    profile: str | CapabilityProfile | None = None,
    working_dir: str | None = None,
    auto_approve: bool | None = None,
    enable_delegate: bool | None = None,
    session_id: str | None = None,
) -> RuntimeRegistryBuild:
    """Resolve options/profile, then build and extend a runtime registry."""
    capability_profile = resolve_profile(profile if profile is not None else runtime_options.profile)
    updated_options = replace(
        runtime_options,
        profile=capability_profile,
        working_dir=runtime_options.working_dir if working_dir is None else working_dir,
        auto_approve=runtime_options.auto_approve if auto_approve is None else auto_approve,
        enable_delegate=(
            runtime_options.enable_delegate if enable_delegate is None else enable_delegate
        ),
        session_id=runtime_options.session_id if session_id is None else session_id,
    )

    registry = ToolFactory(capability_profile).create_registry(working_dir=updated_options.working_dir)
    register_runtime_tools(
        registry,
        runtime_session=runtime_session,
        runtime_options=updated_options,
        approval_callback=approval_callback,
        cancel_check=cancel_check,
        parent_agent_cancel_check=parent_agent_cancel_check,
    )
    return RuntimeRegistryBuild(
        capability_profile=capability_profile,
        runtime_options=updated_options,
        registry=registry,
    )
