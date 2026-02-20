# rho-agent

A configurable agent runtime for software development, research, and operations.

<!-- ![demo](assets/demo.gif) -->

rho-agent is a configurable runtime for deploying AI agents across software development, debugging, and operations workflows. It provides a structured agent loop with built-in tool handlers for shell execution, file inspection, database access, and external service integration — all governed by capability profiles that control what each agent can and cannot do.

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
```

## Python API

Embed agents in services, workers, and batch systems using the runtime directly:

```python
import asyncio
from rho_agent import RuntimeOptions, create_runtime, run_prompt

async def main() -> None:
    runtime = create_runtime(
        "You are a research assistant.",
        options=RuntimeOptions(profile="developer", working_dir="/tmp/work"),
    )
    await runtime.start()
    try:
        result = await run_prompt(runtime, "Analyze recent failures.")
        print(result.text, result.status, result.usage)
    except Exception:
        raise
    finally:
        await runtime.close(result.status if "result" in dir() else "error")

asyncio.run(main())
```

See the [Runtime API](docs/site/runtime-api.md) docs and [`examples/`](examples/) for more patterns including parallel dispatch and cancellation.

## Capability Profiles

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
- **Multi-agent coordination** — delegate subtasks to child agents or connect running agents for cross-context collaboration through the monitor
- **Observability** — session tracking, token usage, and tool execution metrics with SQLite (default) or PostgreSQL backends, session labels, and an interactive CLI monitor (`rho-agent monitor`)
- **Session management** — list, pause, resume, and kill running agents from another terminal (`rho-agent ps`, `rho-agent kill`)
- **Remote sandboxing** — execute all tools in a Daytona cloud VM with `--profile daytona`
- **Evaluation integrations** — [BIRD-Bench](rho_agent/eval/birdbench/) (text-to-SQL) and [TerminalBench](rho_agent/eval/harbor/) via Harbor

## Documentation

| | |
|---|---|
| [Quickstart](docs/site/quickstart.md) | Get running in minutes |
| [Installation](docs/site/installation.md) | Environment setup and install options |
| [CLI Reference](docs/site/cli-reference.md) | Commands, flags, and usage examples |
| [Runtime API](docs/site/runtime-api.md) | Programmatic Python interface for embedding agents |
| [Tools](docs/site/tools.md) | Complete tool handler reference |
| [Profiles](docs/site/profiles.md) | Capability profiles and custom YAML |
| [Observability](docs/site/observability.md) | Telemetry, monitor, and dashboard |
| [Architecture](docs/site/architecture.md) | System design, agent loop, and signal protocol |

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
