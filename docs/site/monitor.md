---
title: Monitor
description: Watch, control, and steer running agents from another terminal.
order: 12
---

The monitor gives you a live view into running agent sessions. You can watch an agent work in real time, answer its tool approval requests, pause it, inject new instructions mid-run, or cancel it — all from a separate terminal.

## Getting started

Start an agent in one terminal:

```bash
# Terminal 1 — run an agent
rho-agent main --profile developer "migrate the sync client to async and update all callers"
```

Open the monitor in another terminal:

```bash
# Terminal 2 — monitor it
rho-agent monitor
```

With no argument the monitor reads the CLI's own sessions in `~/.config/rho-agent/sessions`. Pass a directory to monitor sessions created elsewhere (for example by the Python SDK). The monitor opens an interactive command loop. Type `help` to see available commands.

## Commands

### `ps` — list sessions

Shows the 50 most recently active sessions, one table per project (the session's working directory). Sessions spawned by another session (`delegate` and agent tools) are listed under their parent. `Active` is the time since the session last did anything, and `Cost` is its spend so far including its children.

```
monitor> ps
~/proj/api-server
┌────────────┬──────────────────┬────────────┬─────────┬────────┬───────┬──────────────────────────────────┐
│ Session    │ Status           │ Model      │ Started │ Active │  Cost │ Preview                          │
├────────────┼──────────────────┼────────────┼─────────┼────────┼───────┼──────────────────────────────────┤
│ a3f8c1d2   │ running          │ gpt-5-mini │   4m12s │     3s │ $0.42 │ migrate the sync client to async │
│ └ 51c09e7a │ waiting_approval │ gpt-5-mini │   1m05s │    40s │ $0.08 │ update the callers in billing/   │
│ 7b2e09aa   │ completed        │ gpt-5-mini │   1h02m │    58m │ $0.11 │ find all TODO comments           │
└────────────┴──────────────────┴────────────┴─────────┴────────┴───────┴──────────────────────────────────┘
```

An `Owner` column (`user@host`) appears when the directory holds sessions from more than one account or machine.

| Status | Meaning |
|---|---|
| `created` | Session exists but has not started a run |
| `running` | Working: calling the model or running tools |
| `waiting_input` | Interactive session idle at its prompt |
| `waiting_approval` | Blocked on a tool approval |
| `paused` | Held by a `pause` request |
| `completed`, `error`, `cancelled`, `max_turns`, `budget_exceeded` | How the last run ended |
| `dead` | Recorded as live, but its process no longer exists (killed or crashed) |
| `unknown` | Recorded as live with no process recorded, so it cannot be checked (sessions from older versions) |

`dead` is detected by checking the recorded process ID, which only works for sessions started on the machine you are monitoring from. A session from another host shows its recorded status as is.

### `watch <prefix> [n|all]` — follow a session live

Shows the last 20 events of `trace.jsonl`, then renders new events as the agent works. Pass a number to see more or fewer past events, or `all` for the whole trace. You see LLM calls, tool invocations, tool results, and assistant messages in real time. Press Ctrl+C to stop watching (the agent keeps running). The prefix can be a child session's ID, to follow one subagent.

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
- **Child sessions** starting and ending
- **Approval requests** and how they were answered

### `approvals`, `approve`, `deny` — answer tool approval requests

An agent with nobody at its terminal cannot ask "Approve? [Y/n]". Instead it files the request and waits, with status `waiting_approval`. `ps` tells you when requests are pending, and `approvals` lists them:

```
monitor> approvals
┌─────────┬──────────┬─────────┬────────────────────────────────────┐
│ Request │ Session  │ Waiting │ Tool call                          │
├─────────┼──────────┼─────────┼────────────────────────────────────┤
│ 47e7d3  │ a3f8c1d2 │   2m10s │ bash(alembic upgrade head)         │
└─────────┴──────────┴─────────┴────────────────────────────────────┘

monitor> approve 47e
approved: 47e7d3 (bash, a3f8c1d2)
```

`approve <request>` lets the tool call run. `deny <request>` rejects it: the agent is told the call was rejected and its run ends, as when you answer "n" at a terminal prompt. `approve all` approves everything pending. Requests from dead sessions are not listed.

Approvals are queued this way when the agent is started without a terminal (in the background, from a script or scheduler), or with `--queue-approvals`:

```bash
rho-agent main --profile developer --queue-approvals "run the migration and fix what breaks"
```

Started in a terminal without that flag, the agent prompts in that terminal as before. `--auto-approve` skips approvals entirely. A subagent asks through its parent, so its requests are queued or prompted the same way the parent's are.

### `pause <prefix>` — pause a session

Writes a `pause` sentinel file. The agent finishes the tool calls of its current turn and pauses before its next model call; its status changes to `paused`. Use this when you want to review what the agent is doing before it continues.

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

Appends an instruction to `directives.jsonl`. The agent picks it up before its next model call and reads it as a user message. Use this to course-correct without restarting.

What happens depends on what the session is doing:

- **In a run**: read before the next model call.
- **Interactive session idle at its prompt** (`waiting_input`): it starts a turn with the directive straight away. Anything half-typed at that prompt is discarded.
- **Finished or dead**: there is no process to read it. The monitor says so, and the directive is read when the session is resumed with `rho-agent -r <id>`.

```
monitor> directive a3f focus on the database module first, the API layer can wait
directive queued for a3f8c1
```

Directives are useful for:
- Redirecting the agent's focus ("skip the tests for now, fix the migration first")
- Providing information the agent doesn't have ("the database password is in vault, not env vars")
- Adjusting scope ("don't refactor the utils module, just update the imports")

### `cancel <prefix|all>` — stop a session

Writes a `cancel` sentinel file. The agent stops its current run, and the request is removed once that run has ended, so the session can be resumed later. Only live sessions are cancelled: finished and dead sessions are left alone.

```
monitor> cancel a3f
cancel: a3f8c1d2

monitor> cancel 7b2
7b2e09aa: not running (completed)

monitor> cancel all
cancel: a3f8c1d2
```

An interactive session that is cancelled this way ends, including when it is idle at its prompt; pressing Ctrl+C inside the session only stops the current turn. A session waiting for an approval stops waiting.

### Prefix matching

All commands that take a session ID accept a prefix. If the prefix is ambiguous (matches multiple sessions), the monitor tells you and lists the matches. Use `all` with `cancel` and `pause` to target every live session. A prefix can also match a child session (the indented IDs in `ps`), so you can watch, pause, steer or cancel one subagent. Cancelling or pausing a parent applies to its children too.

## How it works

The monitor operates entirely through the filesystem. Each session directory contains sentinel files and append-only logs:

| File | Role |
|---|---|
| `trace.jsonl` | Append-only event log — `watch` tails this file |
| `meta.json` | Registry record (status, owner, project, usage) — `ps` reads this |
| `cancel` | Sentinel — `cancel` touches this file, the agent removes it when the run ends |
| `pause` | Sentinel — `pause` touches it, `resume` removes it |
| `directives.jsonl` | Append-only — `directive` appends here, agent reads between turns |
| `approvals/` | Pending approval requests and their answers — `approve` and `deny` write here |
| `children/<id>/` | Sessions spawned by this one, each with the same layout |

No database, no network transport, no daemon process. The agent and the monitor coordinate through the session directory.
