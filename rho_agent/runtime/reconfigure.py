"""Runtime reconfiguration helpers."""

from __future__ import annotations

from dataclasses import replace

from ..capabilities import CapabilityProfile
from ..capabilities.factory import ToolFactory
from .factory import resolve_profile
from .registry_extensions import register_runtime_tools
from .types import AgentRuntime


def reconfigure_runtime(
    runtime: AgentRuntime,
    *,
    profile: str | CapabilityProfile | None = None,
    working_dir: str | None = None,
    auto_approve: bool | None = None,
    enable_delegate: bool | None = None,
) -> CapabilityProfile:
    """Rebuild runtime registry/options and atomically swap active tool set."""
    capability_profile = resolve_profile(profile if profile is not None else runtime.options.profile)
    updated_options = replace(
        runtime.options,
        profile=capability_profile,
        working_dir=runtime.options.working_dir if working_dir is None else working_dir,
        auto_approve=runtime.options.auto_approve if auto_approve is None else auto_approve,
        enable_delegate=(
            runtime.options.enable_delegate if enable_delegate is None else enable_delegate
        ),
    )

    registry = ToolFactory(capability_profile).create_registry(working_dir=updated_options.working_dir)
    register_runtime_tools(
        registry,
        runtime_session=runtime.session,
        runtime_options=updated_options,
        approval_callback=runtime.approval_callback,
        cancel_check=runtime.cancel_check,
    )

    runtime.registry = registry
    runtime.agent.set_registry(registry)
    runtime.profile_name = capability_profile.name
    runtime.options = updated_options

    if runtime.observability and runtime.observability.context:
        runtime.observability.context.profile = capability_profile.name

    return capability_profile
