# rho-agent

rho-agent runs AI agents that read code, run commands and edit files. Agents run inside an OS sandbox: they can change the project they work in, and ask before touching anything outside it. You can use it two ways:

- **From the terminal.** Work with an agent in a conversation, hand it a task to finish on its own, or start several and supervise them from one place.
- **From Python.** Import `rho_agent` and build agents into your own programs. The CLI is a thin layer over the same three classes.

![An interactive session: the agent finds a bug behind two failing tests, fixes it and reruns the tests](docs/demo/cli.gif)

## Install

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and an API key from a model provider that offers an OpenAI-compatible API: OpenAI, OpenRouter, xAI, Groq, DeepSeek and many others. A server you run yourself (vLLM, Ollama) works too. rho-agent is not on PyPI yet, so install it from GitHub.

For the command-line tool:

```bash
uv tool install git+https://github.com/smith-nathanh/rho-agent
```

This puts `rho-agent` and `rho-eval` on your `PATH`.

To use it as a library, add it to your project instead:

```bash
uv add git+https://github.com/smith-nathanh/rho-agent
```

Then tell it which provider to use. rho-agent reads three variables, and only these. It never picks up `OPENAI_API_KEY` or another key you set for a different tool.

| Variable | What to put there |
|---|---|
| `RHO_AGENT_API_KEY` | The API key from your provider |
| `RHO_AGENT_BASE_URL` | Your provider's endpoint. Leave it unset for OpenAI |
| `RHO_AGENT_MODEL` | The model name as your provider spells it. Default: `gpt-6.1-terra` |

For OpenAI:

```bash
export RHO_AGENT_API_KEY=sk-...
```

For another provider, set the endpoint and a model too. OpenRouter, for example:

```bash
export RHO_AGENT_API_KEY=sk-or-...
export RHO_AGENT_BASE_URL=https://openrouter.ai/api/v1
export RHO_AGENT_MODEL=anthropic/claude-haiku-4.5
```

| Provider | `RHO_AGENT_BASE_URL` | Example `RHO_AGENT_MODEL` |
|---|---|---|
| OpenAI | leave unset | `gpt-6.1-terra` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-haiku-4.5` |
| xAI | `https://api.x.ai/v1` | `grok-4.7` |
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Cerebras | `https://api.cerebras.ai/v1` | `gpt-oss-120b` |
| vLLM or Ollama on your machine | e.g. `http://localhost:8000/v1` | the model you serve (no key needed) |

`--model` and `--base-url` on the command line override the variables for one run. A `.env` file in the directory you run from works too. See [Installation](docs/site/installation.md) for extras (database drivers, Daytona, evals) and the other install options.

Models are called through the Responses API, so they reason between tool calls; `--reasoning-effort` (or `AgentConfig.reasoning_effort`) sets how much. That works with OpenAI and with other providers that implement Responses, such as OpenRouter, xAI, Groq and DeepSeek. If a server has no Responses endpoint (Gemini's OpenAI-compatible API, Cerebras, llama.cpp), rho-agent switches to Chat Completions on its own. The [FAQ](docs/site/faq.md) has the details.

## Quick start

In the terminal, go to a project and start a conversation:

```bash
cd ~/proj/myapp
rho-agent
```

```
> where is the retry logic for failed webhooks, and what does it do on the third failure?
```

In Python, the same thing is a few lines. Save this as `ask.py` in a project and run it with `uv run python ask.py`:

```python
import asyncio

from rho_agent import Agent, AgentConfig, Session


async def main() -> None:
    config = AgentConfig(
        system_prompt="You answer questions about the code in the current directory.",
        access="read-only",
    )
    async with Session(Agent(config)) as session:
        result = await session.run("At what quantity does the bulk discount start? One sentence.")
        print(result.text)
        print(result.status)


asyncio.run(main())
```

Run in a copy of this repository's [`docs/demo/projects/orders`](docs/demo/projects/orders), it prints:

```
The bulk discount starts at 11 units in the implementation, though the documentation says 10.
completed
```

The Python example uses `read-only` access, which can look but not change anything. The default, `workspace`, lets the agent edit the project; see [Access](#access). The rest of this page covers the terminal first, then [Python](#in-python).

## In the terminal

### Work with an agent

Run `rho-agent` in a project to start a conversation. The agent can edit files there and run commands, inside a sandbox.

```bash
cd ~/proj/myapp
rho-agent
```

```
> test_webhook has been flaky in CI. find the race condition and fix it
> now run the whole suite
```

When the agent needs something outside the project, such as writing a sibling repository, it asks at the prompt and waits for your answer. Ctrl+C stops the current turn without ending the session, `/help` lists the commands available inside it, and the status bar shows how many tokens the session has used.

Sessions are saved as you go, so you can leave and come back:

```bash
rho-agent --list          # saved sessions
rho-agent -r latest       # pick up the most recent one
```

The agent reads `AGENTS.md` (or `CLAUDE.md`) from the project for instructions, and can hand parts of a job to subagents that run in parallel.

### Hand off a task

Give a prompt on the command line and the agent runs it to completion and exits.

```bash
rho-agent \
  "migrate sync_client.py to async with aiohttp, update all callers, run the tests"

# Write the final answer to a file
rho-agent "summarise the error handling in this repo" --output summary.md

# A reusable prompt file with variables
rho-agent --prompt examples/log_debugger/debug.md --var log_path=/mnt/logs/12345
```

[Prompt files](docs/site/prompt-files.md) are Markdown with a little frontmatter, for tasks you run repeatedly with different inputs.

### Run several and supervise them

Agents started in the background keep working on their own. `rho-agent monitor` shows all of them, grouped by project, with subagents listed under the agent that started them.

![The monitor: two agents in different projects wait for approval, get approved, and one is redirected while it works](docs/demo/monitor.gif)

From the monitor you can:

- see which agents are working, waiting for input, waiting for an approval, or dead
- `watch` one live, including a single subagent
- answer approval requests with `approve` and `deny`
- `pause`, `resume` and `cancel`
- send a `directive` that the agent reads before its next step

An agent with no terminal attached cannot ask you anything directly, so when it needs access outside its sandbox it queues the request and waits for an answer in the monitor:

```bash
rho-agent "split payments don't add up to the invoice total. fix it" &
```

There is no daemon and no server behind this. Each session is a directory of plain files under `~/.config/rho-agent/sessions`, and the monitor reads and writes those files. See [Monitor](docs/site/monitor.md).

## In Python

There are three objects. An `Agent` is configuration and tools, and can be reused. A `Session` is one conversation with it. The session's `State` is the full record of what happened: messages, token usage and status.

This program lets an agent change files, prints each tool call as it happens, then asks a follow-up in the same conversation:

```python
import asyncio

from rho_agent import Agent, AgentConfig, AgentEvent, SessionStore


def show(event: AgentEvent) -> None:
    if event.type == "tool_start":
        print(f"  {event.tool_name}")


async def main() -> None:
    agent = Agent(AgentConfig())  # workspace access: edits the current directory
    session = SessionStore().create_session(agent)  # saved, so the monitor sees it

    fixed = await session.run("The pricing tests fail. Fix the bug.", on_event=show)
    print(fixed.text)

    checked = await session.run("Run the tests and report the result in one line.")
    print(checked.text)
    print(session.state.usage)


asyncio.run(main())
```

```
  bash
  bash
  read
  read
  bash
  edit
  bash
Fixed the bulk discount to apply at the threshold in `pricing.py:12`.

All 4 pricing tests pass.
4 tests passed.
{'input_tokens': 27114, 'output_tokens': 799, 'cached_tokens': 22489, 'reasoning_tokens': 9, 'cost_usd': 0.0}
```

A few things to know:

- **The agent works in the directory your program runs from.** With the default `workspace` access it can change anything there, so try it on a copy of a project first.
- **No one is asked anything by default.** A session created in Python stays inside its sandbox, and commands that need more just fail. Set `session.approval_callback` to let the agent ask for access outside it.
- **`Session(agent)` keeps nothing on disk.** `SessionStore().create_session(agent)` saves the session under `~/.config/rho-agent/sessions`, where `rho-agent monitor`, `rho-agent --list` and `rho-agent -r` find it.
- **Leave out `system_prompt`** to get the same built-in prompt the CLI uses, or pass your own text or a prompt file.

The [Python SDK](docs/site/python-sdk.md) guide covers every `AgentConfig` field, the event types, cancellation and custom tools. There are complete programs in [`examples/`](examples/): a single agent, several agents working in parallel, and a Streamlit app that streams an agent's work into a chat window.

## Access

One setting decides what an agent can change: `--access` on the command line, `AgentConfig(access=...)` in Python.

| Access | The agent can write | For |
|---|---|---|
| `workspace` (default) | The working directory, temp directories and package caches | Everyday work in a project |
| `read-only` | Temp directories only | Investigating without changing anything |
| `full` | Anywhere you can; no sandbox | Containers, CI and other disposable machines |

`read-only` and `workspace` run every command inside the operating system's sandbox: Seatbelt on macOS (built in), [bubblewrap](https://github.com/containers/bubblewrap) on Linux (`sudo apt install bubblewrap`). The kernel enforces the limits, so they hold whatever the command is and for everything it starts:

- Writes outside the writable directories fail, and `.git/hooks` and `.git/config` stay read-only even inside the project.
- Credentials such as `~/.ssh`, `~/.aws` and `~/.config/gh` are unreadable, and API keys are removed from commands' environment.
- Local services that run outside the sandbox, such as Docker, ssh-agent and tmux, are unreachable over their Unix sockets.
- The network is on; `network: false` turns it off.

The file tools check the same rules themselves. If the sandbox can't run, rho-agent stops rather than run unsandboxed; `rho-agent sandbox check` tests it. Windows has no sandbox yet (use WSL2).

When the agent needs more, it asks: *"Agent wants write access outside the sandbox: ~/proj/shared-lib. Allow for this session? [y/N]"*. You answer in the terminal, or in `rho-agent monitor` for agents running in the background. With nobody to ask, as in a Python program without an approval callback, it stays inside the sandbox.

```bash
rho-agent --access read-only
rho-agent --add-dir ../shared-lib      # allow this directory up front
rho-agent --access full                # inside a container
```

Inside a session, `/access` shows what the agent can reach and `/access read-only` changes it. See [Access](docs/site/access.md) for settings and how the sandbox works on each platform.

The sandbox stops an agent changing things outside the project by mistake. It doesn't yet stop every deliberate escape: on macOS a command can still ask another app to act for it, local TCP services are reachable while the network is on, and there's no per-domain network allowlist. See [Known limits](docs/site/access.md#known-limits), and [docs/sandboxing-known-issues.md](docs/sandboxing-known-issues.md) for details and planned fixes.

## What else is in the box

The built-in tools cover shell, file reading and editing, search, images and Excel files, plus query tools for SQLite, PostgreSQL, MySQL, Oracle and Vertica. See [Tools](docs/site/tools.md).

With `--backend daytona`, the shell and file tools run in a [Daytona](docs/site/daytona.md) cloud sandbox instead of on your machine.

Every session writes a `trace.jsonl` of each model call, tool call and result. You can read it with `jq`, replay it in Python, or [export it](rho_agent/export/README.md) to Harbor's ATIF trajectory format. See [Observability](docs/site/observability.md).

The same traces export as OpenTelemetry spans, following the GenAI semantic conventions, for tools such as Langfuse, Arize Phoenix, LangSmith or any OTLP collector. A run becomes an agent span with its model calls and tool calls under it, and child agents are nested under the tool call that started them. Message content is left out unless you ask for it. Send a finished session with `rho-agent export <session-id> --format otlp --endpoint <url>`, or set `RHO_AGENT_OTLP_ENDPOINT` to send spans while the agent runs. Protobuf transport needs the `otel` extra (`uv pip install -e ".[otel]"`). See [OpenTelemetry export](docs/site/observability.md#opentelemetry-export).

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
| [Access](docs/site/access.md) | What agents can change, the sandbox, and access requests |
| [Daytona](docs/site/daytona.md) | Remote sandbox execution via Daytona |
| [Observability](docs/site/observability.md) | Session traces, offline inspection, observers, and OpenTelemetry export |
| [Export](rho_agent/export/README.md) | Convert traces to ATIF for training data or OTLP for observability tools |
| [Monitor](docs/site/monitor.md) | Watch, control, and steer running agents |
| [Prompt files](docs/site/prompt-files.md) | Reusable prompts with variables |
| [Continuum](docs/site/continuum.md) | Build a project from a requirements document across many sessions |
| [FAQ](docs/site/faq.md) | Models, reasoning, endpoints and common problems |

## Development

```bash
git clone https://github.com/smith-nathanh/rho-agent.git
cd rho-agent
uv sync --group dev
uv run pytest
```

The recordings in this README are made by [`docs/demo/record.sh`](docs/demo/record.sh) against a real model, using the small projects in `docs/demo/projects`.

## License

MIT. See [LICENSE](LICENSE).
