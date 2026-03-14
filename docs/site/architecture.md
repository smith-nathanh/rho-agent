---
title: Architecture
description: System design, session lifecycle, agent loop, tool routing, and multi-agent coordination.
order: 4
---

## Overview

rho-agent separates *what an agent is* from *what an agent does* from *what an agent has done*.

**Agent** is a stateless blueprint — a system prompt, a permission profile, and a tool registry bundled into a reusable definition. It holds no conversation history and runs nothing on its own. You create one Agent and stamp out as many independent Sessions from it as you need. The registry is built once at construction time and shared across Sessions, so parallel conversations don't duplicate setup work or invalidate each other's prompt caches.

**Session** is where execution happens. It drives the agent loop — LLM call, tool dispatch, repeat — and owns all the lifecycle concerns: cancellation, pause/resume via sentinel files, operator directives injected between turns, auto-compaction when context gets large, and budget gates. Sessions are long-lived. You can call `run()` multiple times for multi-turn conversations, and resume them across process restarts from a saved trace.

**State** is the portable conversation record — messages, cumulative token usage, and cost. Every mutation is appended to `trace.jsonl` immediately, so State is crash-safe with no explicit save step. A trace file can be replayed into a full State object with `State.from_jsonl()`, which is how session resume works and how the monitor reads a running session from another process. State is also observable: attach `StateObserver` instances to stream events to external systems in real time.

The **CLI** ties these together — Typer commands for launching agents, listing saved sessions, resuming past work, and running the live monitor.

```
CLI (rho-agent)
    └── Agent (stateless blueprint)
            └── Session (execution + lifecycle)
                    ├── State (messages, usage, trace)
                    ├── ToolRegistry (handler dispatch)
                    └── LLM Client (model calls)
```

This decomposition is deliberate. Agent is stateless so one definition can back many concurrent sessions without shared mutable state. State is a standalone data object so it can be serialized, replayed, inspected offline, or handed to a monitor — all without a live Session or Agent. Session is the only piece with a lifecycle, which keeps cancellation, pause, and cleanup concerns in one place instead of spread across the system.

## Session lifecycle

The lifecycle is simple:

1. **`Agent(config)`** — Resolves the permission profile, builds the tool registry, resolves the system prompt.
2. **`Session(agent)`** — Sync constructor. Creates the State, sets up defaults. Ready to run immediately.
3. **`await session.run(prompt=...)`** — Executes a prompt to completion. Returns a `RunResult`. Can be called multiple times for multi-turn conversations.
4. **`await session.close()`** — Cleans up remote resources (Daytona backend only). No-op for local backend.

```python
agent = Agent(config)
session = Session(agent)
result = await session.run(prompt="Analyze the logs.")
# session is still open for more runs
result2 = await session.run(prompt="Summarize findings.")
```

## Agent loop

`Session.run()` drives the agent loop internally. The flow for each call:

1. Add the user message to State
2. Check if auto-compact should trigger (based on `context_window` and `auto_compact` settings)
3. Send messages to the model via the LLM client
4. Process the model response:
   - **Text response** — append to State, emit `text` events
   - **Tool calls** — dispatch each tool through the registry, emit `tool_start`/`tool_end` events, append results to State
5. If tool calls were made, loop back to step 3 (model sees tool results)
6. When the model produces a final text response with no tool calls, the loop ends
7. Return a `RunResult` with the final text, all events, status, and per-run usage

Events are emitted throughout the loop via `State` observers and the `on_event` callback pattern. This enables real-time display, tracing, and custom side channels.

## Tool routing

The `ToolRegistry` maps tool names to handler instances. It is built during `Agent()` construction based on the active `PermissionProfile`:

- **`readonly`** registers: `bash` (restricted), `read`, `grep`, `glob`, `list`, `read_excel`, and available database handlers
- **`developer`** adds: `write`, `edit`, `delegate`
- **`eval`** enables database mutations and disables approval
- When `--backend daytona` is set, file and shell tools execute in a Daytona cloud sandbox instead of locally
- Custom profiles define their own tool set via YAML

Each handler implements a common interface: `name`, `description`, `parameters` (JSON schema), and `handle()`. The registry generates the tool definitions array sent to the model API.

The `ToolFactory` creates the registry from a profile, resolving which handlers to instantiate and how to configure them.

## Permission enforcement

Permissions are enforced at two levels:

**Tool availability** — Tools not registered in the registry don't exist from the model's perspective. A `readonly` agent never sees `write` or `edit` in its tool list.

**Runtime checks** — Handlers perform additional validation at execution time. `BashHandler` checks commands against its allowlist in restricted mode. Database handlers reject mutation queries in readonly mode. `WriteHandler` blocks overwrites and sensitive paths in create-only mode.

## Approval system

When a permission profile requires approval, tool calls are routed through an approval callback before execution. Set the callback on the Session:

```python
session.approval_callback = my_callback
```

The callback receives the tool name and arguments and returns an allow/deny decision. In interactive CLI mode, this prompts the user. In the Python API, a custom callback can implement any approval logic. If approval is denied, a `tool_blocked` event is emitted.

When the approval callback is not set and approval is required, the session raises an `ApprovalInterrupt` exception for out-of-band handling.

## Multi-agent coordination

### Delegation

The `delegate` tool spawns a child agent to handle a focused subtask. The child:

- Inherits the parent's profile, model, and working directory
- Optionally receives a copy of the parent's conversation history (`full_context`)
- Cannot delegate further (single-level only)
- Is automatically cancelled when the parent is cancelled

### Session control protocol

Running agents are controlled through sentinel files in the session directory (`~/.config/rho-agent/sessions/<session_id>/`):

| File | Purpose |
|---|---|
| `cancel` | Presence signals cancellation request |
| `pause` | Presence signals pause request |
| `directives.jsonl` | Queued operator directives (JSON lines, append-only) |

The [Monitor](monitor/) reads `trace.jsonl` for live observation and writes sentinel files to control running agents.

## Conversation persistence

`SessionStore` manages session directories at `~/.config/rho-agent/sessions/`. Each session directory contains:

| File | Purpose |
|---|---|
| `config.yaml` | AgentConfig snapshot for the session |
| `trace.jsonl` | Append-only event log (all events from all runs) |
| `meta.json` | Session metadata (id, status, created_at, model, profile, first_prompt) |
| `cancel` | Cancel sentinel (created when cancellation requested) |
| `pause` | Pause sentinel (created when pause requested) |
| `directives.jsonl` | Operator directives (JSON lines) |

`trace.jsonl` is the source of truth. `State.from_jsonl()` replays a trace file to fully restore conversation state, enabling resume across process restarts.

The `--list` flag shows saved sessions and `--resume` restores one, enabling long-running investigations across multiple sessions.

## Key directories

| Path | Contents |
|---|---|
| `rho_agent/cli/` | Typer CLI commands |
| `rho_agent/core/` | Core abstractions: `agent`, `session`, `state`, `config`, `events`, `session_store` |
| `rho_agent/tools/` | Tool handlers and registry |
| `rho_agent/permissions/` | Permission profile definitions and enforcement |
| `rho_agent/client/` | LLM client adapters |
| `rho_agent/prompts/` | Prompt template loading and variable substitution |
