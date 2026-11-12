---
title: Observability
description: Session traces, offline inspection, and custom observers.
order: 11
---

Observability is built into the core. `State` automatically writes `trace.jsonl` to the session directory whenever you use `SessionStore`. There is nothing to enable or configure — tracing is always on.

## trace.jsonl format

Every significant event during a session is appended as a JSON line to `trace.jsonl`. Each line has a `type` field and a `ts` timestamp.

### Event types

| Type | Description |
|---|---|
| `run_start` | A new `session.run()` call begins |
| `run_end` | A `session.run()` call completes (includes status) |
| `message` | A message added to conversation (role: user, assistant, tool, system) |
| `llm_start` | LLM API call initiated |
| `llm_end` | LLM API call completed (includes token counts and cost) |
| `tool_start` | Tool execution begins (includes tool name and args) |
| `tool_end` | Tool execution completes (includes `success`) |
| `child_start` / `child_end` | A child session was spawned / finished (includes `child_session_id`) |
| `tool_blocked` | Tool call denied by permissions or approval |
| `compact` | Context compaction occurred (includes tokens before/after and `method`: `summary` or `prune`) |
| `usage` | Cumulative usage snapshot |

### Sample trace

```jsonl
{"type": "run_start", "run_id": 1, "prompt": "List all tables.", "ts": "2026-02-21T10:00:00Z"}
{"type": "message", "role": "user", "content": "List all tables.", "ts": "2026-02-21T10:00:00Z"}
{"type": "llm_start", "model": "gpt-5-mini", "context_size": 150, "ts": "2026-02-21T10:00:00Z"}
{"type": "llm_end", "model": "gpt-5-mini", "input_tokens": 150, "output_tokens": 45, "cache_read_tokens": 0, "cost_usd": 0.0003, "ts": "2026-02-21T10:00:01Z"}
{"type": "tool_start", "tool_name": "bash", "tool_call_id": "call_abc", "tool_args": {"command": "psql -c '\\dt'"}, "ts": "2026-02-21T10:00:01Z"}
{"type": "tool_end", "tool_name": "bash", "tool_call_id": "call_abc", "success": true, "ts": "2026-02-21T10:00:01Z"}
{"type": "llm_start", "model": "gpt-5-mini", "context_size": 280, "ts": "2026-02-21T10:00:02Z"}
{"type": "llm_end", "model": "gpt-5-mini", "input_tokens": 280, "output_tokens": 90, "cache_read_tokens": 0, "cost_usd": 0.0006, "ts": "2026-02-21T10:00:03Z"}
{"type": "message", "role": "assistant", "content": "The database has 12 tables...", "ts": "2026-02-21T10:00:03Z"}
{"type": "run_end", "run_id": 1, "status": "completed", "ts": "2026-02-21T10:00:03Z"}
```

## Session directory layout

Each session directory at `~/.config/rho-agent/sessions/<session_id>/` contains:

| File | Purpose |
|---|---|
| `config.yaml` | AgentConfig snapshot (model, profile, system prompt, etc.) |
| `trace.jsonl` | Append-only event log — source of truth for the session |
| `meta.json` | Registry record for the session (see below) |
| `cancel` | Sentinel file — presence cancels the current run, then it is removed |
| `pause` | Sentinel file — presence signals pause request |
| `directives.jsonl` | Operator directives queued for the agent (JSON lines) |
| `children/<id>/` | Sessions spawned by this one, each with the same layout |

### meta.json

`meta.json` is written by the process running the session and replaced atomically, so it is always safe to read. It is updated at the start and end of every run, before each model call, and when the session starts or stops waiting.

| Field | Description |
|---|---|
| `id`, `parent_session_id` | Session ID, and the spawning session's ID for a child |
| `status` | See the [status table](monitor/#ps--list-sessions) |
| `waiting_on` | Tool name while `status` is `waiting_approval` |
| `model`, `profile` | Agent configuration |
| `cwd` | Working directory — the project the session belongs to |
| `pid`, `host`, `user` | Process, machine and account running the session |
| `started_at`, `last_activity` | ISO timestamps |
| `first_prompt` | First 200 characters of the first prompt |
| `run_count`, `input_tokens`, `output_tokens`, `cost_usd` | Usage so far, children included |

A killed process cannot record a final status, so readers should not trust a live `status` on its own. `rho_agent.core.session_meta.effective_status(meta)` checks the recorded `pid` and returns `dead` when the process is gone.

## Monitor

For live observation and control of running agents, see the [Monitor](monitor/) guide.

## Offline inspection

`trace.jsonl` files are plain JSON lines and can be inspected with standard tools.

### Using jq

```bash
# Count tool calls in a session
jq -s '[.[] | select(.type == "tool_start")] | length' trace.jsonl

# Show token counts and cost for each LLM call
jq 'select(.type == "llm_end") | {input_tokens, output_tokens, cost_usd}' trace.jsonl

# List distinct tool names used
jq -r 'select(.type == "tool_start") | .tool_name' trace.jsonl | sort -u

# Get total input tokens across all LLM calls
jq -s '[.[] | select(.type == "llm_end") | .input_tokens] | add' trace.jsonl
```

### Using State.from_jsonl

Replay a trace file to restore full conversation state in Python:

```python
from pathlib import Path
from rho_agent import State

trace = Path("~/.config/rho-agent/sessions/abc123/trace.jsonl").expanduser()
state = State.from_jsonl(trace.read_bytes())
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
            print(f"LLM call: {event.get('input_tokens', 0)} in, {event.get('output_tokens', 0)} out")
        elif event["type"] == "tool_end":
            print(f"Tool {event['tool_name']} {'ok' if event.get('success') else 'failed'}")

agent = Agent(AgentConfig(profile="developer"))
session = Session(agent)
session.state.add_observer(MetricsCollector())

result = await session.run(prompt="Analyze the codebase.")
```

Observers receive every event that is written to `trace.jsonl`, making them suitable for live dashboards, metrics collection, alerting, or custom logging pipelines.

## ATIF export

Session traces can be exported to [ATIF](https://github.com/laude-institute/harbor) (Agent Trajectory Interchange Format), a structured JSON schema for agent trajectories designed for SFT/RL training data.

```bash
# Export a session to stdout
rho-agent export <session-id> --dir ~/.config/rho-agent/sessions

# Write to a file
rho-agent export <session-id> -d ~/.config/rho-agent/sessions -o trajectory.json
```

```python
from rho_agent.export.atif import trace_to_atif

trajectory = trace_to_atif(
    "path/to/trace.jsonl",
    session_id="abc123",
    model_name="gpt-5-mini",
)
```

The export maps trace events to ATIF steps, groups tool calls with their results, and populates per-step and session-level metrics. See [`rho_agent/export/`](../../rho_agent/export/README.md) for details and the [CLI reference](cli-reference.md#rho-agent-export) for all flags.
