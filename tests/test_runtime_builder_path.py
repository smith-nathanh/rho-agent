from __future__ import annotations

import asyncio
import os

from rho_agent.capabilities import CapabilityProfile
from rho_agent.runtime import RuntimeOptions, create_runtime, reconfigure_runtime
from rho_agent.runtime import builder as runtime_builder

os.environ.setdefault("OPENAI_API_KEY", "test-key")


def test_create_runtime_uses_shared_registry_builder(monkeypatch) -> None:
    called = False
    real_builder = runtime_builder.build_runtime_registry

    def wrapped_build_runtime_registry(**kwargs):
        nonlocal called
        called = True
        return real_builder(**kwargs)

    monkeypatch.setattr(
        "rho_agent.runtime.factory.build_runtime_registry", wrapped_build_runtime_registry
    )

    runtime = create_runtime(
        "system",
        options=RuntimeOptions(
            profile=CapabilityProfile.readonly(),
            enable_delegate=False,
        ),
    )

    assert called is True
    assert runtime.profile_name == "readonly"
    assert runtime.options.profile.name == "readonly"


def test_reconfigure_runtime_uses_shared_registry_builder(monkeypatch) -> None:
    runtime = create_runtime(
        "system",
        options=RuntimeOptions(
            profile=CapabilityProfile.readonly(),
            enable_delegate=False,
        ),
    )

    called = False
    real_builder = runtime_builder.build_runtime_registry

    def wrapped_build_runtime_registry(**kwargs):
        nonlocal called
        called = True
        return real_builder(**kwargs)

    monkeypatch.setattr(
        "rho_agent.runtime.reconfigure.build_runtime_registry",
        wrapped_build_runtime_registry,
    )

    profile = reconfigure_runtime(
        runtime,
        profile="developer",
        working_dir=".",
        auto_approve=False,
    )

    assert called is True
    assert profile.name == "developer"
    assert runtime.profile_name == "developer"
    assert runtime.options.profile.name == "developer"
    assert runtime.options.working_dir == "."
    assert runtime.options.auto_approve is False


def test_reconfigure_runtime_updates_default_approval_callback() -> None:
    runtime = create_runtime(
        "system",
        options=RuntimeOptions(
            profile=CapabilityProfile.readonly(),
            auto_approve=True,
            enable_delegate=False,
        ),
    )

    assert runtime.approval_callback is not None
    assert asyncio.run(runtime.approval_callback("bash", {})) is True

    reconfigure_runtime(runtime, auto_approve=False)

    assert runtime.approval_callback is not None
    assert runtime.options.auto_approve is False
    assert asyncio.run(runtime.approval_callback("bash", {})) is False


def test_reconfigure_runtime_preserves_explicit_approval_callback() -> None:
    async def explicit_callback(_: str, __: dict[str, object]) -> bool:
        return True

    runtime = create_runtime(
        "system",
        options=RuntimeOptions(
            profile=CapabilityProfile.readonly(),
            auto_approve=False,
            enable_delegate=False,
        ),
        approval_callback=explicit_callback,
    )

    reconfigure_runtime(runtime, auto_approve=False)

    assert runtime.approval_callback is explicit_callback
    assert asyncio.run(runtime.approval_callback("bash", {})) is True
