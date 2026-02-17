"""Runtime execution helpers."""

from __future__ import annotations

from .protocol import Runtime
from .store import RunStore
from .types import EventHandler, RunResult, RunState, ToolApprovalItem


async def run_prompt(
    runtime: Runtime,
    prompt: str | RunState,
    *,
    on_event: EventHandler | None = None,
    approval_decisions: dict[str, bool] | None = None,
) -> RunResult:
    """Run one prompt and collect output/events.

    If ``prompt`` is a :class:`RunState`, the runtime is restored and
    execution resumes from that interrupted state.
    """
    pending_tool_calls: list[tuple[str, str, dict[str, object]]] | None = None
    user_input = ""
    if isinstance(prompt, RunState):
        runtime.restore_state(prompt)
        pending_tool_calls = [
            (item.tool_call_id, item.tool_name, dict(item.tool_args))
            for item in prompt.pending_approvals
        ]
    else:
        user_input = prompt

    if pending_tool_calls is None and approval_decisions is None:
        events = runtime.agent.run_turn(user_input)
    else:
        try:
            events = runtime.agent.run_turn(
                user_input,
                pending_tool_calls=pending_tool_calls,
                approval_overrides=approval_decisions,
            )
        except TypeError as exc:
            if isinstance(prompt, RunState):
                raise TypeError(
                    "Runtime agent does not support resuming from RunState."
                ) from exc
            events = runtime.agent.run_turn(user_input)
    status = "completed"
    collected_text: list[str] = []
    collected_events = []
    usage: dict[str, int | float] = {}

    try:
        if runtime.observability:
            wrapped_prompt = prompt if isinstance(prompt, str) else "[resumed]"
            events = runtime.observability.wrap_turn(events, wrapped_prompt)

        async for event in events:
            collected_events.append(event)
            if event.type == "text" and event.content:
                collected_text.append(event.content)
            elif event.type == "turn_complete" and event.usage:
                usage = event.usage
            elif event.type == "cancelled":
                status = "cancelled"
            elif event.type == "interruption":
                status = "interrupted"
            elif event.type == "error":
                status = "error"

            if on_event:
                maybe_awaitable = on_event(event)
                if maybe_awaitable is not None:
                    await maybe_awaitable
    except Exception:
        status = "error"
        raise

    interruptions: list[ToolApprovalItem] = []
    state = None
    if status == "interrupted":
        interruptions = _consume_interruptions(runtime)
        state = runtime.capture_state(interruptions)

    return RunResult(
        text="".join(collected_text),
        events=collected_events,
        status=status,
        usage=usage,
        interruptions=interruptions,
        state=state,
    )


async def run_prompt_stored(
    runtime: Runtime,
    prompt: str | RunState | None,
    *,
    run_store: RunStore,
    run_id: str,
    on_event: EventHandler | None = None,
    approval_decisions: dict[str, bool] | None = None,
) -> RunResult:
    """Run a prompt with automatic state persistence.

    Wraps :func:`run_prompt` with store-based load/save:

    * If ``prompt`` is ``None``, loads the interrupted state from ``run_store``.
    * On interruption, saves state to ``run_store``.
    * On completion/cancellation/error, deletes state from ``run_store``.
    """
    if prompt is None:
        loaded = run_store.load(run_id)
        if loaded is None:
            raise ValueError(f"No persisted run state found for run_id={run_id!r}.")
        prompt = loaded

    result = await run_prompt(
        runtime,
        prompt,
        on_event=on_event,
        approval_decisions=approval_decisions,
    )

    if result.status == "interrupted" and result.state is not None:
        run_store.save(run_id, result.state)
    elif result.status in {"completed", "cancelled", "error"}:
        run_store.delete(run_id)

    return result


def _consume_interruptions(runtime: Runtime) -> list[ToolApprovalItem]:
    """Fetch and normalize pending approval calls from the runtime agent."""
    pending = runtime.agent.consume_interrupted_tool_calls()
    return [
        ToolApprovalItem(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
        )
        for tool_call_id, tool_name, tool_args in pending
    ]
