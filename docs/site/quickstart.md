---
title: Quickstart
description: Get rho-agent running in minutes.
order: 2
---

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- An OpenAI-compatible API key (set `OPENAI_API_KEY`)

## Install

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync
```

## Start the command center (recommended)

```bash
export OPENAI_API_KEY=sk-...
uv run rho-agent
```

This launches the **command center TUI** (Textual). From here you can browse sessions/telemetry and manage running agents.

## Start an interactive agent session (non-TUI)

```bash
uv run rho-agent main
```

This starts a REPL with the default `readonly` profile. The agent can inspect files and run read-only shell commands, but cannot modify anything.

## Start a development session

```bash
uv run rho-agent main --profile developer --working-dir ~/proj/myapp
```

The `developer` profile enables file editing, unrestricted shell access, and the full tool suite.

## Run a one-shot task

Pass a prompt as a positional argument to run a single task and exit:

```bash
uv run rho-agent main "list all Python files that import asyncio"
```

## Use a prompt template

Prompt files are markdown documents with YAML frontmatter for variables:

```bash
uv run rho-agent main --prompt examples/job-failure.md \
  --var cluster=prod \
  --var log_path=/mnt/logs/123
```

See [Prompt Files](prompt-files/) for the full template format.

## Connect to a database

Set the appropriate environment variables and the database tools become available automatically:

```bash
export SQLITE_DB=/path/to/data.db
uv run rho-agent main "list all tables and describe their schemas"
```

Database tools support PostgreSQL, MySQL, Oracle, Vertica, and SQLite. See [Tools](tools/) for configuration details.

## Manage running agents

```bash
# Command center (TUI)
uv run rho-agent

# Or from another terminal:
uv run rho-agent ps
uv run rho-agent kill <prefix>
```

Note: the legacy `monitor` and `dashboard` commands still exist, but the command center TUI is the primary workflow.

## Next steps

- [Installation](installation/) — all install methods and environment configuration
- [CLI Reference](cli-reference/) — complete command and flag documentation
- [Profiles](profiles/) — understand and customize capability profiles
- [Runtime API](runtime-api/) — embed agents in Python services
