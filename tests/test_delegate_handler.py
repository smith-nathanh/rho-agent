from __future__ import annotations

from types import SimpleNamespace

import pytest

from rho_agent.capabilities import CapabilityProfile
from rho_agent.core.session import Session
from rho_agent.runtime.options import RuntimeOptions
from rho_agent.runtime.types import RunResult
from rho_agent.runtime.factory import create_runtime
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.delegate import DelegateHandler


@pytest.mark.asyncio
async def test_empty_instruction_returns_error_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_session = Session(system_prompt="system")
    parent_options = RuntimeOptions(profile=CapabilityProfile.readonly(), session_id="parent-session")
    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=None,
        parent_cancel_check=None,
        parent_agent_cancel_check=None,
        requires_approval=False,
    )

    called = False

    def fake_create_runtime(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)

    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "   "})
    )

    assert output.success is False
    assert called is False


@pytest.mark.asyncio
async def test_delegate_full_context_false_uses_empty_child_history(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_session = Session(system_prompt="system")
    parent_session.history = [{"role": "user", "content": "existing context"}]
    parent_options = RuntimeOptions(
        profile=CapabilityProfile.readonly(),
        session_id="parent-session",
        telemetry_metadata={"trace_id": "abc123"},
    )
    async def parent_approval_callback(tool_name: str, tool_args: dict[str, object]) -> bool:
        return True

    def parent_cancel_check() -> bool:
        return False

    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=parent_approval_callback,
        parent_cancel_check=parent_cancel_check,
        parent_agent_cancel_check=None,
        requires_approval=False,
    )

    captured: dict[str, object] = {}
    close_calls: list[str] = []

    def fake_create_runtime(
        system_prompt: str,
        *,
        options: RuntimeOptions | None = None,
        session: Session | None = None,
        approval_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> object:
        captured["system_prompt"] = system_prompt
        captured["options"] = options
        captured["session"] = session
        captured["approval_callback"] = approval_callback
        captured["cancel_check"] = cancel_check
        return SimpleNamespace()

    async def fake_start_runtime(runtime: object) -> None:
        captured["started"] = runtime

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        captured["prompt"] = prompt
        return RunResult(text="child complete", events=[], status="completed", usage={"input_tokens": 1})

    async def fake_close_runtime(runtime: object, status: str = "completed") -> None:
        close_calls.append(status)

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.close_runtime", fake_close_runtime)

    output = await handler.handle(
        ToolInvocation(
            call_id="1",
            tool_name="delegate",
            arguments={"instruction": "do the thing", "full_context": False},
        )
    )

    child_options = captured["options"]
    child_session = captured["session"]
    assert isinstance(child_options, RuntimeOptions)
    assert isinstance(child_session, Session)
    assert child_session.history == []
    assert child_options.enable_delegate is False
    assert child_options.session_id is None
    assert child_options.telemetry_metadata["trace_id"] == "abc123"
    assert child_options.telemetry_metadata["parent_session_id"] == "parent-session"
    assert captured["approval_callback"] is parent_approval_callback
    assert callable(captured["cancel_check"])
    assert captured["cancel_check"]() is False
    assert output.success is True
    assert output.content == "child complete"
    assert output.metadata["child_usage"] == {"input_tokens": 1}
    assert output.metadata["child_status"] == "completed"
    assert "child_session_id" in output.metadata
    assert output.metadata["duration_seconds"] >= 0
    assert close_calls == ["completed"]


@pytest.mark.asyncio
async def test_delegate_full_context_true_snapshots_parent_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_session = Session(system_prompt="system")
    parent_session.history = [
        {"role": "assistant", "tool_calls": [{"id": "abc", "function": {"name": "read"}}]}
    ]
    parent_options = RuntimeOptions(profile=CapabilityProfile.readonly(), session_id="parent-session")
    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=None,
        parent_cancel_check=None,
        parent_agent_cancel_check=None,
        requires_approval=False,
    )

    captured_child_session: Session | None = None

    def fake_create_runtime(
        system_prompt: str,
        *,
        options: RuntimeOptions | None = None,
        session: Session | None = None,
        approval_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> object:
        nonlocal captured_child_session
        captured_child_session = session
        return SimpleNamespace()

    async def fake_start_runtime(runtime: object) -> None:
        return None

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        return RunResult(text="ok", events=[], status="completed", usage={})

    async def fake_close_runtime(runtime: object, status: str = "completed") -> None:
        return None

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.close_runtime", fake_close_runtime)

    await handler.handle(
        ToolInvocation(
            call_id="1",
            tool_name="delegate",
            arguments={"instruction": "continue", "full_context": True},
        )
    )

    assert captured_child_session is not None
    assert captured_child_session.history == parent_session.history
    assert captured_child_session.history is not parent_session.history
    assert captured_child_session.history[0] is not parent_session.history[0]
    parent_session.history[0]["role"] = "changed"
    assert captured_child_session.history[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_delegate_failure_still_closes_child_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_session = Session(system_prompt="system")
    parent_options = RuntimeOptions(profile=CapabilityProfile.readonly(), session_id="parent-session")
    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=None,
        parent_cancel_check=None,
        parent_agent_cancel_check=None,
        requires_approval=False,
    )

    close_calls: list[str] = []

    def fake_create_runtime(*args: object, **kwargs: object) -> object:
        return SimpleNamespace()

    async def fake_start_runtime(runtime: object) -> None:
        return None

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        raise RuntimeError("boom")

    async def fake_close_runtime(runtime: object, status: str = "completed") -> None:
        close_calls.append(status)

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.close_runtime", fake_close_runtime)

    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "do work"})
    )

    assert output.success is False
    assert output.metadata["child_status"] == "error"
    assert "child_session_id" in output.metadata
    assert output.metadata["duration_seconds"] >= 0
    assert close_calls == ["error"]


def test_child_runtime_does_not_include_delegate_tool() -> None:
    runtime = create_runtime(
        "system",
        options=RuntimeOptions(
            profile=CapabilityProfile.readonly(),
            enable_delegate=False,
        ),
    )
    assert "delegate" not in runtime.registry


@pytest.mark.asyncio
async def test_delegate_child_cancel_check_includes_parent_agent_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_session = Session(system_prompt="system")
    parent_options = RuntimeOptions(profile=CapabilityProfile.readonly(), session_id="parent-session")
    parent_cancelled = False

    def parent_agent_cancel_check() -> bool:
        return parent_cancelled

    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=None,
        parent_cancel_check=None,
        parent_agent_cancel_check=parent_agent_cancel_check,
        requires_approval=False,
    )

    captured_cancel_check = None

    def fake_create_runtime(
        system_prompt: str,
        *,
        options: RuntimeOptions | None = None,
        session: Session | None = None,
        approval_callback: object | None = None,
        cancel_check: object | None = None,
    ) -> object:
        del system_prompt, options, session, approval_callback
        nonlocal captured_cancel_check
        captured_cancel_check = cancel_check
        return SimpleNamespace()

    async def fake_start_runtime(runtime: object) -> None:
        del runtime
        return None

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        del runtime, prompt
        return RunResult(text="ok", events=[], status="completed", usage={})

    async def fake_close_runtime(runtime: object, status: str = "completed") -> None:
        del runtime, status
        return None

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.close_runtime", fake_close_runtime)

    await handler.handle(
        ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "do work"})
    )

    assert callable(captured_cancel_check)
    assert captured_cancel_check() is False
    parent_cancelled = True
    assert captured_cancel_check() is True


@pytest.mark.asyncio
async def test_delegate_registers_child_session_in_signal_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_session = Session(system_prompt="system")
    parent_options = RuntimeOptions(profile=CapabilityProfile.readonly(), session_id="parent-session")
    handler = DelegateHandler(
        parent_session=parent_session,
        parent_options=parent_options,
        parent_approval_callback=None,
        parent_cancel_check=None,
        parent_agent_cancel_check=None,
        requires_approval=False,
    )

    class FakeSignalManager:
        def __init__(self) -> None:
            self.registered: list[str] = []
            self.deregistered: list[str] = []

        def register(self, info: object) -> None:
            session_id = getattr(info, "session_id", "")
            self.registered.append(session_id)

        def deregister(self, session_id: str) -> None:
            self.deregistered.append(session_id)

        def is_cancelled(self, session_id: str) -> bool:
            del session_id
            return False

    fake_sm = FakeSignalManager()
    monkeypatch.setattr("rho_agent.tools.handlers.delegate.SignalManager", lambda: fake_sm)

    def fake_create_runtime(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(session_id="child-1234")

    async def fake_start_runtime(runtime: object) -> None:
        del runtime
        return None

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        del runtime, prompt
        return RunResult(text="ok", events=[], status="completed", usage={})

    async def fake_close_runtime(runtime: object, status: str = "completed") -> None:
        del runtime, status
        return None

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)
    monkeypatch.setattr("rho_agent.runtime.lifecycle.close_runtime", fake_close_runtime)

    await handler.handle(
        ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "do work"})
    )

    assert fake_sm.registered == ["child-1234"]
    assert fake_sm.deregistered == ["child-1234"]
