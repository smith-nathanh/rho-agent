---
title: CLI Reference
description: Commands, flags, and usage examples for the rho-agent CLI.
order: 4
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
| `--profile <name\|path>` | Capability profile: `readonly`, `developer`, `eval`, or path to a YAML file |
| `--working-dir <path>` | Set the agent's working directory |
| `--prompt <file.md>` | Load a markdown prompt template with frontmatter |
| `--var key=value` | Set a template variable (repeatable) |
| `--vars-file <file.yaml>` | Load template variables from a YAML file |
| `--system <text>` | Override the system prompt entirely |
| `--model <name>` | Model to use (default: `gpt-5-mini` or `OPENAI_MODEL`) |
| `--base-url <url>` | API endpoint override |
| `--output <path>` | Write the agent's final response to a file |
| `--auto-approve / -y` | Skip tool approval prompts |
| `--resume <id> / -r <id>` | Resume a saved conversation (`-r latest` for most recent) |
| `--list / -l` | List saved conversations |
| `--team-id <id>` | Team ID for observability |
| `--project-id <id>` | Project ID for observability |
| `--observability-config <path>` | Path to `observability.yaml` |
| `--reasoning-effort <level>` | Reasoning effort for o1/o3 models |

### Examples

```bash
# Interactive session with developer profile
rho-agent main --profile developer --working-dir ~/proj/myapp

# One-shot task
rho-agent main "find all TODO comments in the codebase"

# Prompt template with variables
rho-agent main --prompt examples/job-failure.md \
  --var cluster=prod \
  --var log_path=/mnt/logs/123

# Resume the most recent conversation
rho-agent main -r latest

# Write output to a file
rho-agent main "summarize this project" --output summary.md
```

## `rho-agent dashboard`

Launch the Streamlit observability dashboard for browsing sessions, token usage, and tool statistics.

```bash
rho-agent dashboard [OPTIONS]
```

| Flag | Description |
|---|---|
| `--port <port>` | Dashboard port (default: `8501`) |
| `--db <path>` | Path to telemetry SQLite database |

## `rho-agent monitor`

Interactive command center for managing running agents and browsing telemetry.

```bash
rho-agent monitor [OPTIONS]
```

| Flag | Description |
|---|---|
| `--db <path>` | Path to telemetry SQLite database |
| `--limit <n>` | Number of sessions to list (default: `20`) |
| `--read-write` | Open the database in read-write mode |

### Monitor commands

Once inside the monitor, these commands are available:

| Command | Description |
|---|---|
| `overview` | Running agents and active sessions |
| `running` | List running agents |
| `sessions [active\|completed\|all]` | Browse telemetry sessions |
| `show <id_or_prefix>` | Session detail with turns and tool calls |
| `kill <prefix\|all>` | Cancel a running session |
| `pause <prefix\|all>` | Pause a running session |
| `resume <prefix\|all>` | Resume a paused session |
| `directive <prefix> <text>` | Inject a directive into a running agent |
| `connect <a> <b> [more...] -- <task>` | Launch a coordinator across multiple agent contexts |
| `disconnect` | End an active connect session |
| `help` | Show all monitor commands |
| `quit` | Exit the monitor |

## `rho-agent ps`

List running agent sessions.

```bash
rho-agent ps [OPTIONS]
```

| Flag | Description |
|---|---|
| `--cleanup` | Remove stale entries from crashed agents |

## `rho-agent kill`

Cancel running agent sessions.

```bash
rho-agent kill <prefix> [OPTIONS]
```

| Flag | Description |
|---|---|
| `--all` | Kill all running agents |

Pass a session ID prefix to cancel a specific agent, or use `--all` to cancel every running session.
