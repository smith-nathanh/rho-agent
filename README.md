# rho-agent

rho-agent is an agent harness for the terminal. You can work with an agent one-on-one in a CLI, start several on different projects and supervise them from one place, or build agents into your own programs with the Python API. Every agent runs under a permission profile that sets what it is allowed to touch.

![An interactive session: the agent finds a bug behind two failing tests, fixes it and reruns the tests](docs/demo/cli.gif)

## Install

```bash
uv tool install rho-agent
```

This puts `rho-agent` and `rho-eval` on your `PATH`. The agent needs a model key:

```bash
export OPENAI_API_KEY=your-key
export OPENAI_MODEL=gpt-6.1-terra                    # optional, this is the default
export OPENAI_BASE_URL=http://localhost:8000/v1   # optional, for any OpenAI-compatible endpoint
```

A `.env` file in the directory you run from works too. See [Installation](docs/site/installation.md) for extras (database drivers, Daytona, evals) and the other install options.

## Work with an agent

Run `rho-agent` in a project to start a conversation. The default profile is `readonly`, which can look but not change anything; `developer` can edit files and run commands.

```bash
cd ~/proj/myapp
rho-agent --profile developer
```

```
> test_webhook has been flaky in CI. find the race condition and fix it
> now run the whole suite
```

Ctrl+C stops the current turn without ending the session. Sessions are saved as you go, so you can leave and come back:

```bash
rho-agent --list          # saved sessions
rho-agent -r latest       # pick up the most recent one
```

The agent reads `AGENTS.md` (or `CLAUDE.md`) from the project for instructions, and can hand parts of a job to subagents that run in parallel.

## Hand off a task

Give a prompt on the command line and the agent runs it to completion and exits.

```bash
rho-agent --profile developer \
  "migrate sync_client.py to async with aiohttp, update all callers, run the tests"

# Write the final answer to a file
rho-agent "summarise the error handling in this repo" --output summary.md

# A reusable prompt file with variables
rho-agent --prompt examples/log_debugger/debug.md --var log_path=/mnt/logs/12345
```

[Prompt files](docs/site/prompt-files.md) are Markdown with a little frontmatter, for tasks you run repeatedly with different inputs.

## Run several and supervise them

Agents started in the background keep working on their own. `rho-agent monitor` shows all of them, grouped by project, with subagents listed under the agent that started them.

![The monitor: two agents in different projects wait for approval, get approved, and one is redirected while it works](docs/demo/monitor.gif)

From the monitor you can:

- see which agents are working, waiting for input, waiting for an approval, or dead
- `watch` one live, including a single subagent
- answer approval requests with `approve` and `deny`
- `pause`, `resume` and `cancel`
- send a `directive` that the agent reads before its next step

An agent with no terminal attached cannot ask you to approve a command, so it queues the request and waits. In the recording above, both agents were started like this, under a profile that requires approval for every shell command:

```bash
rho-agent --profile careful.yaml "split payments don't add up to the invoice total. fix it" &
```

There is no daemon and no server behind this. Each session is a directory of plain files under `~/.config/rho-agent/sessions`, and the monitor reads and writes those files. See [Monitor](docs/site/monitor.md).

## Python API

The CLI is a thin layer over three objects: an `Agent` (configuration and tools), a `Session` (one conversation), and its `State` (the full record of what happened).

```python
import asyncio

from rho_agent import Agent, AgentConfig, Session


async def main() -> None:
    config = AgentConfig(
        system_prompt="You answer questions about the code in the working directory.",
        profile="readonly",
        working_dir="docs/demo/projects/orders",
    )
    async with Session(Agent(config)) as session:
        result = await session.run("At what quantity does the bulk discount start? Answer in one sentence.")
        print(result.text)
        print(result.status)


asyncio.run(main())
```

```
The bulk discount starts at **11 units** in the current code, although the documentation and test expect it to start at 10.
completed
```

`session.run()` can be called repeatedly on the same session, takes an `on_event` callback for streaming, and returns token usage alongside the text. The [Python SDK](docs/site/python-sdk.md) guide covers events, cancellation, and saving sessions with `SessionStore` so the monitor can see them. There are complete programs in [`examples/`](examples/).

## Permission profiles

A profile decides what an agent can do before it starts. Pass a built-in name or a path to your own YAML file.

| Profile | Shell | File writes | Database | Asks for approval |
|---|---|---|---|---|
| `readonly` (default) | Allowlisted commands only | Off | `SELECT` only | Before shell, database and subagent calls |
| `developer` | Any command | On | `SELECT` only | Before Oracle and MySQL queries |
| `developer-bash-only` | Any command | Through the shell | None | As `developer`; the agent gets only a shell tool |
| `unrestricted` | Any command | On | Full | Never. For containers and sandboxes |

```bash
rho-agent --profile developer
rho-agent --profile path/to/custom-profile.yaml
rho-agent --profile developer --auto-approve     # skip approval prompts
```

See [Profiles](docs/site/profiles.md) for the YAML schema.

## What else is in the box

The built-in tools cover shell, file reading and editing, search, images and Excel files, plus query tools for SQLite, PostgreSQL, MySQL, Oracle and Vertica. See [Tools](docs/site/tools.md).

With `--backend daytona`, the shell and file tools run in a [Daytona](docs/site/daytona.md) cloud sandbox instead of on your machine.

Every session writes a `trace.jsonl` of each model call, tool call and result. You can read it with `jq`, replay it in Python, or [export it](rho_agent/export/README.md) to Harbor's ATIF trajectory format. See [Observability](docs/site/observability.md).

`rho-eval` runs the agent against [BIRD-Bench](rho_agent/eval/birdbench/) (text-to-SQL) and [TerminalBench](rho_agent/eval/harbor/).

## Documentation

| | |
|---|---|
| [Quickstart](docs/site/quickstart.md) | Get running in minutes |
| [Installation](docs/site/installation.md) | Environment setup and install options |
| [Architecture](docs/site/architecture.md) | System design, agent loop, and session protocol |
| [CLI Reference](docs/site/cli-reference.md) | Commands, flags, and usage examples |
| [Python SDK](docs/site/python-sdk.md) | Create and run agents programmatically |
| [Tools](docs/site/tools.md) | Complete tool handler reference |
| [Profiles](docs/site/profiles.md) | Permission profiles and custom YAML |
| [Daytona](docs/site/daytona.md) | Remote sandbox execution via Daytona |
| [Observability](docs/site/observability.md) | Session traces, offline inspection, and observers |
| [Export](rho_agent/export/README.md) | Convert traces to ATIF for training data |
| [Monitor](docs/site/monitor.md) | Watch, control, and steer running agents |

## Development

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync --group dev
uv run pytest
```

The recordings in this README are made by [`docs/demo/record.sh`](docs/demo/record.sh) against a real model, using the small projects in `docs/demo/projects`.
