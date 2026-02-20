---
title: Architecture
description: System design, runtime lifecycle, agent loop, tool routing, and multi-agent coordination.
order: 10
---

## Overview

rho-agent is structured around four layers:

1. **CLI / Runtime entrypoints** — Typer commands for interactive and programmatic execution
2. **Agent loop** — Event-streaming conversation loop with tool routing
3. **Tool handlers** — Shell, file, database, and integration handlers with a common interface
4. **Observability** — Session, turn, and tool-level telemetry

```
CLI / Runtime API
    └── AgentRuntime
            ├── Agent (conversation loop)
            ├── ToolRegistry (handler dispatch)
            ├── Session (conversation state)
            └── ObservabilityProcessor (telemetry)
```

## Runtime lifecycle

1. **`create_runtime()`** — Resolves the capability profile, builds the tool registry, initializes the agent, creates a session, and optionally sets up observability.
2. **`start_runtime()`** — Starts the observability session. Registers the agent with the signal manager for coordination.
3. **`run_prompt()` / `dispatch_prompt()`** — Executes one or more prompts through the agent loop. `run_prompt` blocks; `dispatch_prompt` returns an `AgentHandle` for background execution.
4. **`close_runtime()`** — Closes the observability session with a final status and deregisters from the signal manager.

## Agent loop

Each prompt triggers a turn through the agent loop:

1. User message is added to the session history
2. Session history is sent to the model
3. Model responds with text and/or tool calls
4. Tool calls are routed through the `ToolRegistry` to the appropriate handler
5. Tool results are appended to the session and the model is called again
6. Loop continues until the model produces a final text response with no tool calls

Events are streamed throughout the loop, enabling real-time display and telemetry capture.

## Tool routing

The `ToolRegistry` maps tool names to handler instances. It is built during `create_runtime()` based on the active capability profile:

- **`readonly`** registers: `bash` (restricted), `read`, `grep`, `glob`, `list`, `read_excel`, and available database handlers
- **`developer`** adds: `write`, `edit`, `delegate`
- **`eval`** enables database mutations and disables approval
- **`daytona`** replaces all file and shell tools with remote equivalents that execute in a Daytona cloud VM, while database and external service tools run locally
- Custom profiles define their own tool set via YAML

Each handler implements a common interface: `name`, `description`, `parameters` (JSON schema), and `handle()`. The registry generates the tool definitions array sent to the model API.

## Capability enforcement

Capabilities are enforced at two levels:

**Tool availability** — Tools not registered in the registry simply don't exist from the model's perspective. A `readonly` agent never sees `write` or `edit` in its tool list.

**Runtime checks** — Handlers perform additional validation at execution time. `BashHandler` checks the command against its allowlist in restricted mode. Database handlers reject mutation queries in readonly mode. `WriteHandler` blocks overwrites and sensitive paths in create-only mode.

## Approval system

When the profile's approval mode requires it, tool calls are routed through an approval callback before execution. The callback receives the tool name, arguments, and returns an allow/deny decision. In interactive CLI mode, this prompts the user. In the runtime API, a custom callback can implement any approval logic.

## Multi-agent coordination

### Delegation

The `delegate` tool spawns a child agent to handle a focused subtask. The child:

- Inherits the parent's profile, model, and working directory
- Optionally receives a copy of the parent's conversation history (`full_context`)
- Cannot delegate further (single-level only)
- Is automatically cancelled when the parent is cancelled

This is useful for isolating subtasks that might require many turns or for running focused analyses without polluting the parent's context.

### Signal protocol

By default, agents coordinate through a file-based signal protocol at `~/.config/rho-agent/signals/`. When using the Postgres observability backend, coordination switches to a database-backed transport: agents register in an `agent_registry` table with heartbeats, and signals are delivered via a `signal_queue` table with LISTEN/NOTIFY for instant wakeup. This enables `rho-agent ps` and `rho-agent kill` to work across nodes in a cluster.

**Local mode (default):**

| File | Purpose |
|---|---|
| `<session_id>.running` | Registration (PID, model, instruction, start time) |
| `<session_id>.cancel` | Cancel signal |
| `<session_id>.pause` | Pause signal |
| `<session_id>.directive` | Queued directives (JSON lines) |
| `<session_id>.state` | Latest response state |
| `<session_id>.export` | Context export request |
| `<session_id>.context` | Exported context file |

The signal manager (`SignalManager`) provides methods for registering, deregistering, checking status, sending signals, and queuing directives. The `ps`, `kill`, and `monitor` CLI commands use this protocol.

### Connect

The monitor's `connect` command enables cross-agent collaboration:

1. Operator identifies two or more running agents by session ID prefix
2. Monitor requests context exports from each agent
3. Each agent writes its current context to a `.context` file
4. Monitor combines the contexts and launches a new coordinating agent
5. The coordinator has access to the combined knowledge from all connected agents

This enables workflows where multiple agents have been exploring different aspects of a problem and their findings need to be synthesized.

## Conversation persistence

Sessions are automatically saved to `~/.config/rho-agent/conversations/` as JSON files containing the full message history. The `--list` flag shows saved conversations and `--resume` restores one, enabling long-running investigations across multiple sessions.

## Key directories

| Path | Contents |
|---|---|
| `rho_agent/cli/` | Typer CLI commands |
| `rho_agent/runtime/` | Runtime lifecycle, dispatch, and reconfiguration |
| `rho_agent/tools/` | Tool handlers and registry |
| `rho_agent/capabilities/` | Profile definitions and capability enums |
| `rho_agent/observability/` | Telemetry processing, storage, export, and dashboard |
| `rho_agent/prompts/` | Prompt template loading and variable substitution |
| `rho_agent/agent/` | Agent loop and event streaming |
| `rho_agent/signals/` | Signal manager for agent coordination |
