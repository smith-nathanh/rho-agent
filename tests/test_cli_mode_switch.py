from __future__ import annotations

from types import SimpleNamespace

import pytest

from rho_agent.capabilities import CapabilityProfile
from rho_agent.cli_errors import InvalidProfileError
from rho_agent.cli import handle_command, switch_runtime_profile


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
    runtime = SimpleNamespace(
        registry=None,
        agent=DummyAgent(),
        profile_name="readonly",
        observability=DummyObservability(),
    )

    profile = switch_runtime_profile(runtime, "developer", working_dir=".")

    assert isinstance(profile, CapabilityProfile)
    assert profile.name == "developer"
    assert runtime.registry is runtime.agent.registry
    assert runtime.profile_name == "developer"
    assert runtime.observability.context.profile == "developer"


def test_switch_runtime_profile_invalid_profile_raises_cli_error() -> None:
    runtime = SimpleNamespace(
        registry=None,
        agent=DummyAgent(),
        profile_name="readonly",
        observability=None,
    )

    with pytest.raises(InvalidProfileError) as exc:
        switch_runtime_profile(runtime, "does-not-exist", working_dir=".")

    assert "Invalid profile" in str(exc.value)
