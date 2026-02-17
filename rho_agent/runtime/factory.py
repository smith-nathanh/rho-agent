"""Runtime construction helpers."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from ..client.model import ModelClient
from ..core.agent import Agent
from ..core.session import Session
from ..observability.config import ObservabilityConfig
from ..observability.processor import ObservabilityProcessor
from .builder import build_runtime_registry
from .options import RuntimeOptions
from .protocol import Runtime
from .types import ApprovalCallback, LocalRuntime


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
) -> Runtime:
    """Create a configured runtime.

    Returns a :class:`LocalRuntime` for local profiles, or a
    :class:`DaytonaRuntime` when the resolved profile is ``"daytona"``.
    """
    requested_options = options or RuntimeOptions()
    session_id = requested_options.session_id or str(uuid.uuid4())

    runtime_session = session or Session(system_prompt=system_prompt)

    if approval_callback:
        resolved_approval_callback = approval_callback
    elif requested_options.auto_approve:
        resolved_approval_callback = _auto_approve
    else:
        resolved_approval_callback = _reject_all

    # Build registry/options through the shared builder path used by reconfigure_runtime.
    agent_ref: Agent | None = None

    def parent_agent_cancel_check() -> bool:
        if agent_ref is None:
            return False
        return agent_ref.is_cancelled()

    build = build_runtime_registry(
        runtime_session=runtime_session,
        runtime_options=requested_options,
        approval_callback=resolved_approval_callback,
        cancel_check=cancel_check,
        parent_agent_cancel_check=parent_agent_cancel_check,
        session_id=session_id,
    )
    options = build.runtime_options
    capability_profile = build.capability_profile
    profile_name = capability_profile.name
    registry = build.registry

    client = ModelClient(
        model=options.model,
        base_url=options.base_url,
        service_tier=options.service_tier,
        reasoning_effort=options.reasoning_effort,
    )

    agent = Agent(
        session=runtime_session,
        registry=registry,
        client=client,
        approval_callback=resolved_approval_callback,
        cancel_check=cancel_check,
    )
    agent_ref = agent
    observability = _build_observability(
        options=options,
        model=options.model,
        profile_name=profile_name,
        session_id=session_id,
    )

    if profile_name == "daytona":
        from .daytona import DaytonaRuntime

        manager = DaytonaRuntime.register_daytona_tools(
            registry,
            working_dir=capability_profile.shell_working_dir or options.working_dir or "/home/daytona",
        )
        return DaytonaRuntime(
            agent=agent,
            session=runtime_session,
            registry=registry,
            model=options.model,
            profile_name=profile_name,
            session_id=session_id,
            options=options,
            approval_callback=resolved_approval_callback,
            cancel_check=cancel_check,
            observability=observability,
            _manager=manager,
        )

    return LocalRuntime(
        agent=agent,
        session=runtime_session,
        registry=registry,
        model=options.model,
        profile_name=profile_name,
        session_id=session_id,
        options=options,
        approval_callback=resolved_approval_callback,
        cancel_check=cancel_check,
        observability=observability,
    )
