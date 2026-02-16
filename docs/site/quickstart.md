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

## Start an interactive session

```bash
export OPENAI_API_KEY=sk-...
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

## Run in a remote sandbox

Use the `daytona` profile to execute all tools in a Daytona cloud VM. The agent process stays local — only tool execution happens remotely.

```bash
uv pip install 'rho-agent[daytona]'
export DAYTONA_API_KEY=your-key
uv run rho-agent main --profile daytona "explore the filesystem and install Python 3.13"
```

A sandbox is provisioned on the first tool call and automatically cleaned up when the session ends.

## Monitor running agents

```bash
# List running agents
uv run rho-agent ps

# Launch the observability dashboard
uv run rho-agent dashboard

# Open the interactive monitor
uv run rho-agent monitor
```

## Next steps

- [Installation](installation/) — all install methods and environment configuration
- [CLI Reference](cli-reference/) — complete command and flag documentation
- [Profiles](profiles/) — understand and customize capability profiles
- [Runtime API](runtime-api/) — embed agents in Python services
