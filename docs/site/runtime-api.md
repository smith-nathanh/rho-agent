---
title: Runtime API
description: Programmatic Python interface for embedding agents in services, workers, and batch systems.
order: 5
---

The runtime API lets you create, dispatch, and manage agent runs from Python code. Use it when embedding agents in services, background workers, or orchestration scripts.

## Basic usage

```python
import asyncio
from rho_agent import RuntimeOptions, close_runtime, create_runtime, run_prompt, start_runtime


async def main() -> None:
    runtime = create_runtime(
        "You are a research assistant.",
        options=RuntimeOptions(
            profile="developer",
            working_dir="/tmp/work",
            team_id="acme",
            project_id="incident-response",
        ),
    )
    await start_runtime(runtime)
    try:
        result = await run_prompt(runtime, "Analyze recent failures and summarize root causes.")
        print(result.text)
    finally:
        await close_runtime(runtime, result.status)


asyncio.run(main())
```

## Core functions

### `create_runtime(system_prompt, options, session, approval_callback, cancel_check)`

Creates a configured runtime with tools, agent, and session. Resolves the capability profile, initializes observability if `team_id` and `project_id` are provided, and returns an `AgentRuntime` bundle.

### `start_runtime(runtime)`

Starts the observability session. Idempotent — safe to call multiple times.

### `run_prompt(runtime, prompt, on_event)`

Executes a single prompt to completion. Returns a `RunResult` with the agent's text response, all events, final status, and token usage. Blocks until the agent finishes.

### `dispatch_prompt(runtime, prompt, on_event, token)`

Dispatches a prompt in the background without blocking. Returns an `AgentHandle` for monitoring and cancellation.

```python
from rho_agent import CancellationToken, dispatch_prompt

token = CancellationToken()
handle = dispatch_prompt(runtime, "analyze the logs", token=token)

# Check status
handle.done()       # bool
handle.status       # "running", "completed", "cancelled", "error"

# Wait for completion
result = await handle.wait()

# Cancel if needed
handle.cancel("timeout exceeded")
```

### `close_runtime(runtime, status)`

Closes the observability session with a final status. Status should be `"completed"`, `"error"`, or `"cancelled"`.

### `reconfigure_runtime(runtime, profile, working_dir, auto_approve, enable_delegate)`

Hot-swaps runtime configuration without creating a new runtime. Rebuilds the tool registry and updates the agent's tools atomically. Useful for switching profiles mid-session.

```python
from rho_agent import reconfigure_runtime

new_profile = reconfigure_runtime(runtime, profile="readonly")
```

## Types

### `RuntimeOptions`

```python
@dataclass
class RuntimeOptions:
    model: str = "gpt-5-mini"
    base_url: str | None = None
    reasoning_effort: str | None = None
    working_dir: str | None = None
    profile: str | CapabilityProfile | None = None
    auto_approve: bool = True
    team_id: str | None = None
    project_id: str | None = None
    observability_config: str | None = None
    session_id: str | None = None
    telemetry_metadata: dict[str, Any] = {}
    enable_delegate: bool = True
```

All fields can be set via environment variables (see [Installation](installation/)). When `session_id` is `None`, a UUID is generated automatically. The `telemetry_metadata` dict is attached to the session for custom tracking (e.g., `{"job_id": "job-123"}`).

### `RunResult`

```python
@dataclass
class RunResult:
    text: str                      # Final text response
    events: list[AgentEvent]       # All events from the turn
    status: str                    # "completed", "error", "cancelled"
    usage: dict[str, int]          # Token counts
```

### `AgentRuntime`

```python
@dataclass
class AgentRuntime:
    agent: Agent
    session: Session
    registry: ToolRegistry
    model: str
    profile_name: str
    session_id: str
    options: RuntimeOptions
    approval_callback: ApprovalCallback | None
    cancel_check: Callable[[], bool] | None
    observability: ObservabilityProcessor | None
```

### `CancellationToken`

```python
class CancellationToken:
    def cancel(reason: str) -> None
    def is_cancelled() -> bool
    @property
    def reason(self) -> str
```

### `AgentHandle`

Returned by `dispatch_prompt()`. Provides methods to monitor and control a background agent run.

```python
class AgentHandle:
    def done() -> bool
    def cancel(reason: str) -> None
    async def wait() -> RunResult
    @property
    def status(self) -> str  # "running", "completed", "cancelled", "error"
```

## Patterns

### Parallel dispatch

Run multiple agents concurrently and collect results:

```python
from rho_agent import (
    RuntimeOptions, create_runtime, start_runtime,
    close_runtime, dispatch_prompt,
)

runtimes = []
handles = []

for task in tasks:
    rt = create_runtime("You are an analyst.", options=RuntimeOptions(
        profile="readonly",
        working_dir=task.working_dir,
    ))
    await start_runtime(rt)
    handle = dispatch_prompt(rt, task.prompt)
    runtimes.append(rt)
    handles.append(handle)

results = [await h.wait() for h in handles]

for rt, result in zip(runtimes, results):
    await close_runtime(rt, result.status)
```

### When to use API vs CLI

Use the runtime API when you need programmatic control: dispatching agents from services, running parallel workloads, or integrating with existing Python systems. Use the CLI for manual exploration, ad hoc investigations, and interactive development sessions.
