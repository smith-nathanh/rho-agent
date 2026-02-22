---
title: FAQ
description: Common questions about setup, usage, profiles, and operations.
order: 11
---

## Setup

### What models are supported?

Any OpenAI-compatible API. Set `OPENAI_API_KEY` and optionally `OPENAI_BASE_URL` to point to your provider. The default model is `gpt-5-mini` (override with `OPENAI_MODEL` or `--model`).

### Do I need Docker?

Not for normal usage. Docker is only required for running Harbor/TerminalBench evaluations, which execute tasks in isolated containers.

### How do I connect to multiple databases?

Set environment variables for each database type. For SQLite, pass a comma-separated list of paths via `SQLITE_DB`. For other databases, each set of connection variables configures one database. The agent selects between them using the `database` parameter in tool calls.

## Usage

### What's the difference between interactive and one-shot mode?

**Interactive mode** (`rho-agent main`) starts a REPL where you type messages and the agent responds in a loop. **One-shot mode** (`rho-agent main "your prompt"`) runs a single task and exits. Both support the same profiles, tools, and observability.

### How do I resume a previous conversation?

```bash
# List saved conversations
rho-agent main --list

# Resume the most recent
rho-agent main -r latest

# Resume by ID
rho-agent main -r abc123
```

Conversations are saved automatically to `~/.config/rho-agent/sessions/`.

### Can I use prompt templates with one-shot mode?

Yes. Combine `--prompt` with a positional argument — the positional argument becomes the initial user message:

```bash
rho-agent main --prompt task.md "focus specifically on OOM errors"
```

### How does the delegate tool work?

The agent can spawn a child agent to handle a focused subtask. The child inherits the parent's profile and model, runs its task, and returns a text result. Delegation is single-level — children cannot delegate further. See [Architecture](architecture/) for details.

## Profiles

### What is the default profile?

`readonly`. The agent can inspect files and run read-only shell commands, but cannot modify files or execute destructive commands.

### When should I use `eval` mode?

Only in sandboxed environments (containers, VMs) where the security boundary is the environment itself. The `eval` profile disables all restrictions and approval prompts. It exists for benchmark execution, not for general use.

### Can I create a profile that allows file writes but restricts the shell?

Yes. Create a custom YAML profile:

```yaml
profile: write-restricted-shell
shell:
  mode: restricted
file_write:
  mode: full
database:
  mode: readonly
approval:
  mode: dangerous
```

```bash
rho-agent main --profile write-restricted-shell.yaml
```

## Daytona remote sandbox

See the [Daytona](daytona/) guide for setup, usage, sandbox configuration, and file upload/download.

## Operations

### How do I see what agents are running?

```bash
rho-agent ps ~/.config/rho-agent/sessions
```

Or use the monitor for a richer view:

```bash
rho-agent monitor ~/.config/rho-agent/sessions
```

### How do I stop a runaway agent?

```bash
# Stop a specific agent by session ID prefix
rho-agent cancel abc1 --dir ~/.config/rho-agent/sessions

# Stop all running agents
rho-agent cancel --all --dir ~/.config/rho-agent/sessions
```

### Where is telemetry data stored?

Telemetry is stored as trace.jsonl files in session directories at `~/.config/rho-agent/sessions/`.

## Docs publishing

### How do docs get published to the website?

Changes merged to `main` under `docs/site/**` trigger a dispatch workflow that tells the site repo to rebuild.

### Why are my docs changes not visible on the site?

Check that:

1. Changes were committed and pushed to `main`
2. The `rho-agent` workflow ran successfully
3. The site dispatch workflow ran successfully
4. Any deploy hook secrets are configured correctly

### Can I keep internal notes in the docs directory?

Yes. Only files in `docs/site/` are published to the website. Internal notes can live at other paths like `docs/internal/`.
