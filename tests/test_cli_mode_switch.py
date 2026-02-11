from __future__ import annotations

from types import SimpleNamespace

import pytest

from rho_agent.capabilities import CapabilityProfile
from rho_agent.cli import InvalidProfileError, handle_command, switch_runtime_profile
from rho_agent.core.session import Session
from rho_agent.runtime.options import RuntimeOptions


class DummyContext:
    def __init__(self) -> None:
        self.profile = "readonly"


class DummyObservability:
    def __init__(self) -> None:
        self.context = DummyContext()


class DummyAgent:
    def __init__(self) -> None:
        self.registry = None

    def set_registry(self, registry: object) -> None:
        self.registry = registry


def test_handle_command_mode_returns_mode_action() -> None:
    action = handle_command("/mode developer", approval_handler=SimpleNamespace())
    assert action == "mode"


def test_switch_runtime_profile_updates_runtime_and_observability() -> None:
    profile = CapabilityProfile.readonly()
    options = RuntimeOptions(profile=profile, working_dir=".")
    runtime = SimpleNamespace(
        registry=None,
        agent=DummyAgent(),
        session=Session(system_prompt="system"),
        profile_name="readonly",
        options=options,
        approval_callback=None,
        cancel_check=None,
        observability=DummyObservability(),
    )

    profile = switch_runtime_profile(runtime, "developer", working_dir=".")

    assert isinstance(profile, CapabilityProfile)
    assert profile.name == "developer"
    assert runtime.registry is runtime.agent.registry
    assert runtime.profile_name == "developer"
    assert runtime.options.profile.name == "developer"
    assert runtime.observability.context.profile == "developer"
    assert "delegate" in runtime.registry


def test_switch_runtime_profile_invalid_profile_raises_cli_error() -> None:
    profile = CapabilityProfile.readonly()
    options = RuntimeOptions(profile=profile, working_dir=".")
    runtime = SimpleNamespace(
        registry=None,
        agent=DummyAgent(),
        session=Session(system_prompt="system"),
        profile_name="readonly",
        options=options,
        approval_callback=None,
        cancel_check=None,
        observability=None,
    )

    with pytest.raises(InvalidProfileError) as exc:
        switch_runtime_profile(runtime, "does-not-exist", working_dir=".")

    assert "Invalid profile" in str(exc.value)
