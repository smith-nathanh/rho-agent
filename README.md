# rho-agent

A configurable agent runtime for software development, research, and operations.

<!-- ![demo](assets/demo.gif) -->

rho-agent is a configurable runtime for deploying AI agents across software development, debugging, and operations workflows. It provides a structured agent loop with built-in tool handlers for shell execution, file inspection, database access, and external service integration — all governed by permission profiles that control what each agent can and cannot do.

## Quickstart

```bash
# Install globally (adds rho-agent and rho-eval to PATH)
uv tool install .

# Or for local development
uv sync
```

```bash
# Interactive debugging session
rho-agent --profile developer --working-dir ~/proj/myapp
> test_webhook has been flaky in CI — find the race condition and fix it
> now run the tests and make sure it passes

# Investigate a production database interactively
POSTGRES_HOST=db.internal rho-agent --profile readonly
> correlate the 3am latency spike with recent deployments and slow queries
> which tables are missing indexes?

# One-shot: migrate a module and run tests
rho-agent --profile developer --working-dir ~/proj/myapp \
  "migrate sync_client.py to async using aiohttp, update all callers, run tests"

# Triage a failed job using a prompt template
rho-agent --prompt examples/job-failure.md --var cluster=prod --var log_path=/mnt/logs/12345

# Use a saved agent config
rho-agent --config configs/research-assistant.yaml "Analyze recent failures."
```

## Python API

Embed agents in services, workers, and batch systems:

```python
import asyncio
from rho_agent import Agent, AgentConfig, Session

async def main() -> None:
    config = AgentConfig(
        system_prompt="You are a research assistant.",
        profile="developer",
        working_dir="/tmp/work",
    )
    agent = Agent(config)
    session = Session(agent)
    result = await session.run(prompt="Analyze recent failures.")
    print(result.text, result.status, result.usage)

asyncio.run(main())
```

See the [API Reference](docs/site/api-reference.md) docs and [`examples/`](examples/) for more patterns including task-based parallelism and cancellation.

## Permission Profiles

Every agent runs under a profile that controls shell access, file write permissions, and database mutation behavior. The default is `readonly`.

| Profile | Shell | File Write | Database | Use Case |
|---------|-------|------------|----------|----------|
| `readonly` | Restricted (allowlist) | Off | SELECT only | Safe inspection of production systems |
| `developer` | Unrestricted | Full | SELECT only | Local development with file editing |
| `eval` | Unrestricted | Full | Full | Sandboxed benchmark execution |
| `daytona` | Unrestricted (remote) | Full (remote) | SELECT only | Cloud sandbox via Daytona |

```bash
rho-agent --profile readonly
rho-agent --profile developer
rho-agent --profile path/to/custom-profile.yaml
```

Custom profiles are defined in YAML. See [Profiles](docs/site/profiles.md) for the full schema.

## Highlights

- **Native tool handlers** — shell, file read/write/edit, grep, glob, and five database drivers (SQLite, PostgreSQL, MySQL, Oracle, Vertica) with no external plugins or MCP servers
- **Prompt templates** — Markdown with YAML frontmatter and Jinja2 variables for repeatable, parameterized agent tasks
- **Agent configs** — define reusable agent configurations in YAML with `AgentConfig`, load via `--config` on the CLI or `AgentConfig.from_file()` in Python
- **Multi-agent coordination** — delegate subtasks to child agents or interact with running agents through the monitor
- **Observability** — per-session `trace.jsonl` event logs with token usage, tool execution, and timing data; session directories at `~/.config/rho-agent/sessions/`
- **Session management** — monitor, pause, resume, and cancel running agents from another terminal (`rho-agent monitor <dir>`)
- **Remote sandboxing** — execute all tools in a Daytona cloud VM with `--profile daytona`
- **Evaluation integrations** — [BIRD-Bench](rho_agent/eval/birdbench/) (text-to-SQL) and [TerminalBench](rho_agent/eval/harbor/) via Harbor

## Documentation

| | |
|---|---|
| [Quickstart](docs/site/quickstart.md) | Get running in minutes |
| [Installation](docs/site/installation.md) | Environment setup and install options |
| [CLI Reference](docs/site/cli-reference.md) | Commands, flags, and usage examples |
| [API Reference](docs/site/api-reference.md) | Programmatic Python interface for embedding agents |
| [Tools](docs/site/tools.md) | Complete tool handler reference |
| [Profiles](docs/site/profiles.md) | Permission profiles and custom YAML |
| [Observability](docs/site/observability.md) | Telemetry, monitor, and session traces |
| [Architecture](docs/site/architecture.md) | System design, agent loop, and session protocol |

## Development

```bash
uv sync --group dev          # install with dev dependencies
uv run python -m pytest      # run tests
```

## Configuration

```bash
# .env
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-5-mini                   # optional
OPENAI_BASE_URL=http://localhost:8000/v1   # optional
```

Database tools activate automatically when their environment variables are set (e.g. `SQLITE_DB`, `POSTGRES_HOST`). See [Tools](docs/site/tools.md) for the full list.
