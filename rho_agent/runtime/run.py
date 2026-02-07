"""Runtime execution helpers."""

from __future__ import annotations

from .types import AgentRuntime, EventHandler, RunResult


async def run_prompt(
    runtime: AgentRuntime,
    prompt: str,
    *,
    on_event: EventHandler | None = None,
) -> RunResult:
    """Run one prompt and collect output/events."""
    events = runtime.agent.run_turn(prompt)
    status = "completed"
    collected_text: list[str] = []
    collected_events = []
    usage: dict[str, int] = {}
    started = False

    try:
        if runtime.observability:
            await runtime.observability.start_session()
            started = True
            events = runtime.observability.wrap_turn(events, prompt)

        async for event in events:
            collected_events.append(event)
            if event.type == "text" and event.content:
                collected_text.append(event.content)
            elif event.type == "turn_complete" and event.usage:
                usage = event.usage
            elif event.type == "cancelled":
                status = "cancelled"
            elif event.type == "error":
                status = "error"

            if on_event:
                maybe_awaitable = on_event(event)
                if maybe_awaitable is not None:
                    await maybe_awaitable
    except Exception:
        status = "error"
        raise
    finally:
        if runtime.observability and started:
            await runtime.observability.end_session(status)

    return RunResult(
        text="".join(collected_text),
        events=collected_events,
        status=status,
        usage=usage,
    )
