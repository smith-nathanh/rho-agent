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
| `--profile <name\|path>` | Permission profile: `readonly`, `developer`, `eval`, or path to a YAML file |
| `--backend <name>` | Execution backend: `local` (default) or `daytona` |
| `--upload <local:remote>` | Upload files to sandbox (repeatable, format: `./local:/remote`; Daytona only) |
| `--working-dir <path>` | Set the agent's working directory |
| `--system-prompt / -s <file.md>` | Markdown system prompt file with YAML frontmatter |
| `--var key=value` | Set a template variable (repeatable) |
| `--vars-file <file.yaml>` | Load template variables from a YAML file |
| `--prompt / -p <text>` | Prompt text for one-shot mode |
| `--model <name>` | Model to use (default: `gpt-5-mini` or `OPENAI_MODEL`) |
| `--base-url <url>` | API endpoint override |
| `--config / -c <path>` | Agent config YAML file |
| `--output <path>` | Write the agent's final response to a file |
| `--auto-approve / -y` | Skip tool approval prompts |
| `--resume <id> / -r <id>` | Resume a saved conversation (`-r latest` for most recent) |
| `--list / -l` | List saved conversations |
| `--reasoning-effort <level>` | Reasoning effort for o1/o3 models |

### Examples

```bash
# Interactive session with developer profile
rho-agent main --profile developer --working-dir ~/proj/myapp

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
rho-agent monitor <dir>
```

Takes a positional argument pointing to a session directory (e.g. `~/.config/rho-agent/sessions`).

### Monitor commands

Once inside the monitor, these commands are available:

| Command | Description |
|---|---|
| `ps` | List sessions in the directory |
| `watch <prefix>` | Live-follow a session's trace output |
| `cancel <prefix\|all>` | Cancel a running session |
| `pause <prefix\|all>` | Pause a running session |
| `resume <prefix\|all>` | Resume a paused session |
| `directive <prefix> <text>` | Inject a directive into a running agent |
| `help` | Show all monitor commands |
| `quit` | Exit the monitor |

## `rho-agent ps`

List sessions in a directory.

```bash
rho-agent ps <dir>
```

Takes a positional argument pointing to a session directory (e.g. `~/.config/rho-agent/sessions`).

## `rho-agent cancel`

Cancel running agent sessions.

```bash
rho-agent cancel [PREFIX] --dir <dir> [--all]
```

| Flag | Description |
|---|---|
| `--dir <dir>` | Session directory (required) |
| `--all` | Cancel all running agents |

Pass a session ID prefix to cancel a specific agent, or use `--all` to cancel every running session.
