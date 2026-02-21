---
title: Observability
description: Session traces, monitor CLI, and custom observers.
order: 9
---

Observability is built into the core. `State` automatically writes `trace.jsonl` to the session directory whenever you use `SessionStore`. There is nothing to enable or configure — tracing is always on.

## trace.jsonl format

Every significant event during a session is appended as a JSON line to `trace.jsonl`. Each line has a `type` field and a `timestamp`.

### Event types

| Type | Description |
|---|---|
| `run_start` | A new `session.run()` call begins |
| `run_end` | A `session.run()` call completes (includes status) |
| `message` | A message added to conversation (role: user, assistant, tool, system) |
| `llm_start` | LLM API call initiated |
| `llm_end` | LLM API call completed (includes usage) |
| `tool_start` | Tool execution begins (includes tool name and args) |
| `tool_end` | Tool execution completes (includes result) |
| `tool_blocked` | Tool call denied by permissions or approval |
| `compact` | Context compaction occurred (includes tokens before/after) |
| `usage` | Cumulative usage snapshot |

### Sample trace

```jsonl
{"type": "run_start", "run_id": 1, "prompt": "List all tables.", "timestamp": "2026-02-21T10:00:00Z"}
{"type": "message", "role": "user", "content": "List all tables.", "timestamp": "2026-02-21T10:00:00Z"}
{"type": "llm_start", "model": "gpt-5-mini", "message_count": 2, "timestamp": "2026-02-21T10:00:00Z"}
{"type": "llm_end", "usage": {"input_tokens": 150, "output_tokens": 45}, "timestamp": "2026-02-21T10:00:01Z"}
{"type": "tool_start", "tool_name": "bash", "tool_call_id": "call_abc", "args": {"command": "psql -c '\\dt'"}, "timestamp": "2026-02-21T10:00:01Z"}
{"type": "tool_end", "tool_name": "bash", "tool_call_id": "call_abc", "duration_ms": 320, "timestamp": "2026-02-21T10:00:01Z"}
{"type": "llm_start", "model": "gpt-5-mini", "message_count": 4, "timestamp": "2026-02-21T10:00:02Z"}
{"type": "llm_end", "usage": {"input_tokens": 280, "output_tokens": 90}, "timestamp": "2026-02-21T10:00:03Z"}
{"type": "message", "role": "assistant", "content": "The database has 12 tables...", "timestamp": "2026-02-21T10:00:03Z"}
{"type": "run_end", "run_id": 1, "status": "completed", "usage": {"input_tokens": 430, "output_tokens": 135}, "timestamp": "2026-02-21T10:00:03Z"}
```

## Session directory layout

Each session directory at `~/.config/rho-agent/sessions/<session_id>/` contains:

| File | Purpose |
|---|---|
| `config.yaml` | AgentConfig snapshot (model, profile, system prompt, etc.) |
| `trace.jsonl` | Append-only event log — source of truth for the session |
| `meta.json` | Session metadata (id, status, timestamps, model, profile, first prompt) |
| `cancel` | Sentinel file — presence signals cancellation request |
| `pause` | Sentinel file — presence signals pause request |
| `directives.jsonl` | Operator directives queued for the agent (JSON lines) |

## Monitor CLI

The `rho-agent monitor <dir>` command provides live observation and control of running sessions. It operates on a sessions directory (defaults to `~/.config/rho-agent/sessions/`).

### Subcommands

**`rho-agent monitor <dir> ps`** — List running sessions. Shows session ID, status, model, profile, and start time.

**`rho-agent monitor <dir> watch`** — Tail `trace.jsonl` for a session in real time. Streams events as they are appended.

**`rho-agent monitor <dir> cancel <prefix>`** — Cancel a running session by writing a `cancel` sentinel file. The `<prefix>` argument matches session IDs by prefix for convenience.

**`rho-agent monitor <dir> pause <prefix>`** — Pause a running session by writing a `pause` sentinel file. The agent will pause before the next tool execution.

**`rho-agent monitor <dir> resume <prefix>`** — Resume a paused session by removing the `pause` sentinel file.

**`rho-agent monitor <dir> directive <prefix> <message>`** — Append a directive to `directives.jsonl` for a running session. The agent picks up directives between turns.

## Offline inspection

`trace.jsonl` files are plain JSON lines and can be inspected with standard tools.

### Using jq

```bash
# Count tool calls in a session
jq -s '[.[] | select(.type == "tool_start")] | length' trace.jsonl

# Show all LLM usage events
jq 'select(.type == "llm_end") | .usage' trace.jsonl

# List distinct tool names used
jq -r 'select(.type == "tool_start") | .tool_name' trace.jsonl | sort -u

# Get total input tokens across all LLM calls
jq -s '[.[] | select(.type == "llm_end") | .usage.input_tokens] | add' trace.jsonl
```

### Using State.from_jsonl

Replay a trace file to restore full conversation state in Python:

```python
from rho_agent import State

state = State.from_jsonl("~/.config/rho-agent/sessions/abc123/trace.jsonl")
print(f"Messages: {len(state.messages)}")
print(f"Usage: {state.usage}")
print(f"Status: {state.status}")
print(f"Runs completed: {state.run_count}")
```

## Observers

The `StateObserver` protocol lets you attach custom side channels to receive events in real time. Any object with an `on_event(event: dict)` method satisfies the protocol.

```python
from rho_agent import Agent, AgentConfig, Session

class MetricsCollector:
    def on_event(self, event: dict) -> None:
        if event["type"] == "llm_end":
            usage = event.get("usage", {})
            print(f"LLM call: {usage.get('input_tokens', 0)} in, {usage.get('output_tokens', 0)} out")
        elif event["type"] == "tool_end":
            print(f"Tool {event['tool_name']} completed in {event.get('duration_ms', 0)}ms")

agent = Agent(AgentConfig(profile="developer"))
session = Session(agent)
session.state.add_observer(MetricsCollector())

result = await session.run(prompt="Analyze the codebase.")
```

Observers receive every event that is written to `trace.jsonl`, making them suitable for live dashboards, metrics collection, alerting, or custom logging pipelines.
