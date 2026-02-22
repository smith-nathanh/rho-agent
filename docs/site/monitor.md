---
title: Monitor
description: Watch, control, and steer running agents from another terminal.
order: 11
---

The monitor gives you a live view into running agent sessions. You can watch an agent work in real time, pause it, inject new instructions mid-run, or cancel it — all from a separate terminal.

## Getting started

Start an agent in one terminal:

```bash
# Terminal 1 — run an agent
rho-agent main --profile developer "migrate the sync client to async and update all callers"
```

Open the monitor in another terminal:

```bash
# Terminal 2 — monitor it
rho-agent monitor ~/.config/rho-agent/sessions
```

The monitor opens an interactive command loop. Type `help` to see available commands.

## Commands

### `ps` — list sessions

Shows all sessions in the directory with their status, model, elapsed time, and a preview of the initial prompt.

```
monitor> ps
┌──────────┬───────────┬───────────┬─────────┬──────────────────────────────────┐
│ Session  │ Status    │ Model     │ Started │ Preview                          │
├──────────┼───────────┼───────────┼─────────┼──────────────────────────────────┤
│ a3f8c1   │ running   │ gpt-5-mini│ 4m ago  │ migrate the sync client to async │
│ 7b2e09   │ completed │ gpt-5-mini│ 1h ago  │ find all TODO comments           │
└──────────┴───────────┴───────────┴─────────┴──────────────────────────────────┘
```

### `watch <prefix>` — follow a session live

Tails `trace.jsonl` and renders events as the agent works. You see LLM calls, tool invocations, tool results, and assistant messages in real time. Press Ctrl+C to stop watching (the agent keeps running).

```
monitor> watch a3f
Watching a3f8c1 (Ctrl+C to stop)
run_start: migrate the sync client to async and update all callers
llm_start model=gpt-5-mini context=2
llm_end in=1.2k out=89 cost=$0.0012
tool: bash({"command": "find . -name '*.py' | head -20"})
  bash: ok
llm_start model=gpt-5-mini context=4
llm_end in=2.8k out=145 cost=$0.0031
tool: read({"file_path": "src/sync_client.py"})
  read: ok
assistant: I can see the sync client uses requests throughout. Let me identify all callers...
tool: grep({"pattern": "from.*sync_client import", "path": "src/"})
  grep: ok
```

The watch stream shows:
- **LLM calls** with token counts and cost
- **Tool calls** with the tool name and arguments
- **Tool results** with success/error status
- **Assistant messages** as the agent thinks through the task
- **Compaction events** when context is trimmed

### `pause <prefix>` — pause a session

Writes a `pause` sentinel file. The agent pauses before its next tool execution. Use this when you want to review what the agent is doing before it continues.

```
monitor> pause a3f
pause: a3f8c1
```

### `resume <prefix>` — resume a paused session

Removes the `pause` sentinel file so the agent continues.

```
monitor> resume a3f
resume: a3f8c1
```

### `directive <prefix> <message>` — steer the agent mid-run

Appends an instruction to `directives.jsonl`. The agent picks it up between turns and incorporates it as a system message. Use this to course-correct without restarting.

```
monitor> directive a3f focus on the database module first, the API layer can wait
directive queued for a3f8c1
```

Directives are useful for:
- Redirecting the agent's focus ("skip the tests for now, fix the migration first")
- Providing information the agent doesn't have ("the database password is in vault, not env vars")
- Adjusting scope ("don't refactor the utils module, just update the imports")

### `cancel <prefix|all>` — stop a session

Writes a `cancel` sentinel file. The agent stops after its current operation.

```
monitor> cancel a3f
cancel: a3f8c1

monitor> cancel all
cancel: a3f8c1
cancel: 7b2e09
```

### Prefix matching

All commands that take a session ID accept a prefix. If the prefix is ambiguous (matches multiple sessions), the monitor tells you and lists the matches. Use `all` with `cancel` and `pause` to target every session.

## How it works

The monitor operates entirely through the filesystem. Each session directory contains sentinel files and append-only logs:

| File | Role |
|---|---|
| `trace.jsonl` | Append-only event log — `watch` tails this file |
| `meta.json` | Session metadata — `ps` reads status from here |
| `cancel` | Sentinel — `cancel` touches this file, agent checks for it |
| `pause` | Sentinel — `pause` touches it, `resume` removes it |
| `directives.jsonl` | Append-only — `directive` appends here, agent reads between turns |

No database, no network transport, no daemon process. The agent and the monitor coordinate through the session directory.
