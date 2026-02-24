---
title: Quickstart
description: Get rho-agent running in minutes.
order: 2
---

## Prerequisites

- Python 3.13+
- `uv` (recommended)
- An OpenAI-compatible API key (set `OPENAI_API_KEY`)

## Install

CLI install (recommended):

```bash
uv tool install rho-agent
```

Convenience installer:

```bash
curl -fsSL https://rho-agent.dev/install.sh | bash
```

Python SDK / runtime install (in your project directory):

```bash
uv add rho-agent
```

Local development install:

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync
uv tool install .
```

This makes `rho-agent` available as a global command. If you skip `uv tool install`, prefix the commands below with `uv run`.

Use `uv add rho-agent` when you want to import `rho_agent` in your own Python project. `uv tool install` is for CLI commands on your `PATH`.

## Start an interactive session

```bash
export OPENAI_API_KEY=sk-...
rho-agent main
```

This starts a REPL with the default `readonly` profile. The agent can inspect files and run read-only shell commands, but cannot modify anything.

## Start a development session

```bash
rho-agent main --profile developer --working-dir ~/proj/myapp
```

The `developer` profile enables file editing, unrestricted shell access, and the full tool suite.

## Run a one-shot task

Pass a prompt as a positional argument to run a single task and exit:

```bash
rho-agent main "list all Python files that import asyncio"
```

## Use a prompt template

Prompt files are markdown documents with YAML frontmatter for variables:

```bash
rho-agent main --system-prompt examples/job-failure.md \
  --var cluster=prod \
  --var log_path=/mnt/logs/123
```

See [Prompt Files](prompt-files/) for the full template format.

## Connect to a database

Create a database config file at `~/.config/rho-agent/databases.yaml`:

```yaml
databases:
  mydata:
    type: sqlite
    path: /path/to/data.db
```

Then database tools become available automatically:

```bash
rho-agent main "list all tables and describe their schemas"
```

SQLite works out of the box.

- CLI install: `uv tool install 'rho-agent[db]'`
- SDK/project install: `uv add 'rho-agent[db]'`

See [Tools](tools/) for configuration details.

## Run in a remote sandbox

Use the Daytona backend to execute shell and file tools in a remote cloud sandbox. The agent process stays local — only tool execution happens remotely. Combine with any permission profile.

```bash
# CLI install
uv tool install 'rho-agent[daytona]'
export DAYTONA_API_KEY=your-key
rho-agent main --backend daytona --profile developer "explore the filesystem and install Python 3.13"
```

For SDK/project usage, install the extra in your project:

```bash
uv add 'rho-agent[daytona]'
```

A sandbox is provisioned on the first tool call and automatically cleaned up when the session ends.

## Monitor running agents

```bash
# List sessions in a directory
rho-agent ps ~/.config/rho-agent/sessions

# Open the interactive monitor
rho-agent monitor ~/.config/rho-agent/sessions
```

## Next steps

- [Installation](installation/) — all install methods and environment configuration
- [CLI Reference](cli-reference/) — complete command and flag documentation
- [Profiles](profiles/) — understand and customize permission profiles
- [Python SDK](python-sdk/) — create and run agents programmatically
