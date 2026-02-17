"""Runtime execution helpers."""

from __future__ import annotations

from copy import deepcopy

from .types import AgentRuntime, EventHandler, RunResult, RunState, ToolApprovalItem


def _restore_state(runtime: AgentRuntime, state: RunState) -> None:
    """Mutate runtime/session in-place from a serialized run snapshot."""
    runtime.session_id = state.session_id
    runtime.options.session_id = state.session_id
    if runtime.observability:
        runtime.observability.context.session_id = state.session_id
    runtime.session.system_prompt = state.system_prompt
    runtime.session.history = deepcopy(state.history)
    runtime.session.total_input_tokens = state.total_input_tokens
    runtime.session.total_output_tokens = state.total_output_tokens
    runtime.session.total_cached_tokens = state.total_cached_tokens
    runtime.session.total_reasoning_tokens = state.total_reasoning_tokens
    runtime.session.total_cost_usd = state.total_cost_usd
    runtime.session.last_input_tokens = state.last_input_tokens


def _capture_state(runtime: AgentRuntime, interruptions: list[ToolApprovalItem]) -> RunState:
    """Build a serializable run snapshot from the current runtime session."""
    return RunState(
        session_id=runtime.session_id,
        system_prompt=runtime.session.system_prompt,
        history=deepcopy(runtime.session.history),
        total_input_tokens=runtime.session.total_input_tokens,
        total_output_tokens=runtime.session.total_output_tokens,
        total_cached_tokens=runtime.session.total_cached_tokens,
        total_reasoning_tokens=runtime.session.total_reasoning_tokens,
        total_cost_usd=runtime.session.total_cost_usd,
        last_input_tokens=runtime.session.last_input_tokens,
        pending_approvals=interruptions,
    )


def _consume_interruptions(runtime: AgentRuntime) -> list[ToolApprovalItem]:
    """Fetch and normalize pending approval calls from the runtime agent."""
    consume = getattr(runtime.agent, "consume_interrupted_tool_calls", None)
    if not callable(consume):
        return []
    pending = consume()
    return [
        ToolApprovalItem(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
        )
        for tool_call_id, tool_name, tool_args in pending
    ]


async def run_prompt(
    runtime: AgentRuntime,
    prompt: str | RunState,
    *,
    on_event: EventHandler | None = None,
    approval_decisions: dict[str, bool] | None = None,
) -> RunResult:
    """Run one prompt and collect output/events.

    If `prompt` is a `RunState`, the runtime is restored and execution resumes
    from that interrupted state.
    """
    pending_tool_calls: list[tuple[str, str, dict[str, object]]] | None = None
    user_input = ""
    if isinstance(prompt, RunState):
        _restore_state(runtime, prompt)
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
        state = _capture_state(runtime, interruptions)

    return RunResult(
        text="".join(collected_text),
        events=collected_events,
        status=status,
        usage=usage,
        interruptions=interruptions,
        state=state,
    )
