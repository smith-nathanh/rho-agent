---
title: FAQ
description: Common questions about setup, usage, profiles, and operations.
order: 13
---

## Setup

### What models are supported?

OpenAI's models, and any provider with an OpenAI-compatible API (OpenRouter, xAI, Groq, DeepSeek, Cerebras, vLLM, Ollama, ...). Set `RHO_AGENT_API_KEY` to the provider's key and `RHO_AGENT_BASE_URL` to its endpoint. The default model is `gpt-6.1-terra` (override with `RHO_AGENT_MODEL` or `--model`).

### Does the agent use reasoning with OpenAI's GPT-5.6 and GPT-6 models?

Yes. rho-agent calls OpenAI through the Responses API (`/v1/responses`), where these models reason and call tools in the same request. Set the effort with `--reasoning-effort` (or `RHO_AGENT_REASONING_EFFORT` for Harbor runs, or `AgentConfig.reasoning_effort`); leave it unset to use the model's default. Reasoning tokens are counted in the session's usage. A model may still spend none on a simple step, whatever the setting.

Requests are stateless (`store=false`): nothing is kept on OpenAI's side. The model's reasoning comes back encrypted, is saved with the assistant message in `trace.jsonl`, and is sent back on later requests, so a resumed session continues with its reasoning intact.

### Which endpoint is called when I set `RHO_AGENT_BASE_URL`?

The Responses API (`/v1/responses`), the same as for OpenAI. OpenRouter, xAI, Groq, DeepSeek, vLLM and Ollama implement it. If the server answers `/v1/responses` with 404 or 405, which is what Gemini's OpenAI-compatible endpoint, Cerebras and llama.cpp do, rho-agent switches to Chat Completions (`/v1/chat/completions`) for the rest of the session. If a server rejects the `include` field (Groq does), rho-agent stops sending it.

To pin the endpoint, set `RHO_AGENT_API` to `responses` or `chat`; there is then no fallback. `RHO_AGENT_API=chat` skips the first failed request to a server you know has no Responses API. On the Chat Completions path, OpenAI's GPT-5.6 and GPT-6 models refuse tool calls unless reasoning is off, so use the Responses path for them.

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
