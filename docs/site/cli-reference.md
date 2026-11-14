---
title: CLI Reference
description: Commands, flags, and usage examples for the rho-agent CLI.
order: 5
---

## `rho-agent main`

The primary command. Starts an interactive REPL or runs a one-shot task.

```bash
rho-agent main [PROMPT] [OPTIONS]
```

Without a prompt argument, the agent starts in interactive mode. With a prompt, it runs the task and exits.

### Flags

| Flag | Description |
|---|---|
| `--access / -a <level>` | What the agent may change: `workspace` (default), `read-only` or `full`. See [Access](access/) |
| `--add-dir <path>` | Also let the agent write this directory (repeatable) |
| `--backend <name>` | Execution backend: `local` (default) or `daytona` |
| `--upload <local:remote>` | Upload files to sandbox (repeatable, format: `./local:/remote`; Daytona only) |
| `--working-dir <path>` | Set the agent's working directory |
| `--system-prompt / -s <file.md>` | Markdown system prompt file with YAML frontmatter |
| `--var key=value` | Set a template variable (repeatable) |
| `--vars-file <file.yaml>` | Load template variables from a YAML file |
| `--prompt / -p <text>` | Prompt text for one-shot mode |
| `--model <name>` | Model to use (default: `gpt-6.1-terra` or `RHO_AGENT_MODEL`) |
| `--base-url <url>` | API endpoint override |
| `--config / -c <path>` | Agent config YAML file |
| `--output <path>` | Write the agent's final response to a file |
| `--queue-approvals` | Send approval requests to `rho-agent monitor` instead of prompting here (the default when there is no terminal) |
| `--resume <id> / -r <id>` | Resume a saved conversation (`-r latest` for most recent) |
| `--list / -l` | List saved conversations |
| `--reasoning-effort <level>` | Reasoning effort for reasoning models (`none`, `low`, `medium`, `high`); unset uses the model's default |
| `--compact-threshold <fraction>` | Compact history at this fraction of the context window (default `0.7`) |
| `--compact-at-tokens <n>` | Compact history at this exact input token count; wins over the fraction |
| `--no-auto-compact` | Turn automatic history compaction off |

### Examples

```bash
# Interactive session in a project (workspace access)
rho-agent main --working-dir ~/proj/myapp

# Look but don't touch
rho-agent main --access read-only

# One-shot task
rho-agent main "find all TODO comments in the codebase"

# Prompt template with variables
rho-agent main --system-prompt examples/job-failure.md \
  --var cluster=prod \
  --var log_path=/mnt/logs/123

# Resume the most recent conversation
rho-agent main -r latest

# Write output to a file
rho-agent main "summarize this project" --output summary.md
```

## `rho-agent monitor`

Interactive command center for managing running agents.

```bash
rho-agent monitor [dir]
```

Takes an optional session directory. Defaults to the CLI's own sessions in `~/.config/rho-agent/sessions`.

### Monitor commands

Once inside the monitor, these commands are available:

| Command | Description |
|---|---|
| `ps` | List sessions by project, with status, cost and child sessions |
| `watch <prefix> [n\|all]` | Live-follow a session's trace output, starting from the last `n` events (default 20) |
| `approvals` | List pending tool approval requests |
| `approve <request\|all>` | Approve a pending tool call |
| `deny <request>` | Deny a pending tool call |
| `cancel <prefix\|all>` | Cancel the current run of a live session |
| `pause <prefix\|all>` | Pause a live session |
| `resume <prefix\|all>` | Resume a paused session |
| `directive <prefix> <text>` | Inject a directive into a running agent |
| `help` | Show all monitor commands |
| `quit` | Exit the monitor |

## `rho-agent ps`

List sessions by project, with status, last activity, cost and child sessions. See the [Monitor](monitor/) guide for the status values.

```bash
rho-agent ps [dir]
```

Takes an optional session directory. Defaults to the CLI's own sessions in `~/.config/rho-agent/sessions`.

## `rho-agent cancel`

Cancel running agent sessions.

```bash
rho-agent cancel [PREFIX] [--dir <dir>] [--all]
```

| Flag | Description |
|---|---|
| `--dir <dir>` | Session directory (default: `~/.config/rho-agent/sessions`) |
| `--all` | Cancel all live sessions |

Pass a session ID prefix to cancel a specific agent, or use `--all` to cancel every live session. Finished and dead sessions are skipped, and a cancelled session can be resumed later.

## `rho-agent export`

Export a session trace as [ATIF](https://github.com/laude-institute/harbor) JSON (Agent Trajectory Interchange Format), or as OpenTelemetry spans (OTLP) for observability tools. See [Observability — OpenTelemetry export](observability.md#opentelemetry-export).

```bash
rho-agent export <session-id> [OPTIONS]
```

| Flag | Description |
|---|---|
| `--dir <dir>` | Sessions directory (default: `~/.config/rho-agent/sessions`) |
| `--output / -o <path>` | Output file path (default: stdout) |
| `--format / -f <atif\|otlp>` | Output format (default: `atif`) |
| `--endpoint <url>` | `otlp` only: send the spans to this OTLP/HTTP base URL (`/v1/traces` is appended). Headers come from `RHO_AGENT_OTLP_HEADERS` |
| `--protocol <http/protobuf\|http/json>` | `otlp` only: wire format for `--endpoint` (default: `http/protobuf`, needs the `otel` extra) |
| `--include-content` | `otlp` only: include prompts, model output, and tool arguments and results |

### Examples

```bash
# Print ATIF JSON to stdout
rho-agent export abc123 --dir ~/.config/rho-agent/sessions

# Write to a file
rho-agent export abc123 -d ~/.config/rho-agent/sessions -o trajectory.json

# Pipe through jq for inspection
rho-agent export abc123 -d ~/.config/rho-agent/sessions | python -m json.tool

# OpenTelemetry spans as OTLP/JSON
rho-agent export abc123 --format otlp -o spans.json

# Send to a local Phoenix or OpenTelemetry Collector
rho-agent export abc123 --format otlp --endpoint http://localhost:6006
```

## `rho-agent sandbox`

Inspect and try the OS sandbox behind `read-only` and `workspace` access (see [Access](access/)).

```bash
rho-agent sandbox check [--access read-only|workspace] [--network/--no-network] [--add-dir DIR] [-w DIR]
rho-agent sandbox run [same options] -- COMMAND [ARGS...]
```

`check` prints the backend, the resolved policy (what's writable, protected and unreadable, and whether network and Unix sockets are allowed), then runs probes in a scratch directory: writes inside and outside it, git hooks, a credential directory, API keys in the environment, the network, and a Unix socket listening outside the sandbox. It exits 1 if a probe fails or no backend works. `run` runs one command inside the sandbox and exits with its exit code.

`--access` defaults to `workspace`.

```bash
rho-agent sandbox check --access read-only
rho-agent sandbox run -- touch ~/x          # Operation not permitted
rho-agent sandbox run --no-network -- curl https://example.com
rho-agent sandbox run -- docker ps           # refused: Unix sockets are blocked
```
