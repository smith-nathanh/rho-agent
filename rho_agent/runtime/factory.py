"""Runtime construction helpers."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from ..capabilities import CapabilityProfile
from ..capabilities.factory import ToolFactory, load_profile
from ..client.model import ModelClient
from ..core.agent import Agent
from ..core.session import Session
from ..observability.config import ObservabilityConfig
from ..observability.processor import ObservabilityProcessor
from .options import RuntimeOptions
from .types import AgentRuntime, ApprovalCallback


class ObservabilityInitializationError(RuntimeError):
    """Raised when observability cannot be initialized."""

    def __init__(
        self,
        details: str,
        *,
        config_path: str | None,
        team_id: str | None,
        project_id: str | None,
    ) -> None:
        super().__init__(details)
        self.config_path = config_path
        self.team_id = team_id
        self.project_id = project_id


async def _auto_approve(_: str, __: dict[str, object]) -> bool:
    return True


async def _reject_all(_: str, __: dict[str, object]) -> bool:
    return False


def _resolve_profile(
    profile: str | CapabilityProfile | None,
) -> CapabilityProfile:
    if isinstance(profile, CapabilityProfile):
        return profile
    if profile:
        return load_profile(profile)
    return CapabilityProfile.readonly()


def _build_observability(
    options: RuntimeOptions,
    model: str,
    profile_name: str,
    session_id: str,
) -> ObservabilityProcessor | None:
    try:
        config = ObservabilityConfig.load(
            config_path=options.observability_config,
            team_id=options.team_id,
            project_id=options.project_id,
        )
        if not config.enabled or not config.tenant:
            return None

        from ..observability.context import TelemetryContext

        context = TelemetryContext.from_config(config, model=model, profile=profile_name)
        context.session_id = session_id
        context.metadata.update(options.telemetry_metadata)
        return ObservabilityProcessor(config, context)
    except Exception as exc:
        raise ObservabilityInitializationError(
            str(exc),
            config_path=options.observability_config,
            team_id=options.team_id,
            project_id=options.project_id,
        ) from exc


def create_runtime(
    system_prompt: str,
    *,
    options: RuntimeOptions | None = None,
    session: Session | None = None,
    approval_callback: ApprovalCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> AgentRuntime:
    """Create a configured runtime."""
    options = options or RuntimeOptions()
    capability_profile = _resolve_profile(options.profile)
    profile_name = capability_profile.name
    session_id = options.session_id or str(uuid.uuid4())

    runtime_session = session or Session(system_prompt=system_prompt)
    factory = ToolFactory(capability_profile)
    registry = factory.create_registry(working_dir=options.working_dir)
    client = ModelClient(
        model=options.model,
        base_url=options.base_url,
        reasoning_effort=options.reasoning_effort,
    )

    if approval_callback:
        resolved_approval_callback = approval_callback
    elif options.auto_approve:
        resolved_approval_callback = _auto_approve
    else:
        resolved_approval_callback = _reject_all

    agent = Agent(
        session=runtime_session,
        registry=registry,
        client=client,
        approval_callback=resolved_approval_callback,
        cancel_check=cancel_check,
    )
    observability = _build_observability(
        options=options,
        model=options.model,
        profile_name=profile_name,
        session_id=session_id,
    )

    return AgentRuntime(
        agent=agent,
        session=runtime_session,
        registry=registry,
        model=options.model,
        profile_name=profile_name,
        session_id=session_id,
        observability=observability,
    )
