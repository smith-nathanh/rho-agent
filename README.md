# rho-agent

A Python-based agent harness with configurable access/capabilitity profiles.

**Use cases:**
- **Research & inspection**: Investigate logs, database schemas, codebases, and configs
- **Development**: Edit/write code, query databases, run scripts, execute tests - with fine-grained controls over shell commands and SQL restrictions
- **Benchmarking**: Run LLM evaluations (BIRD, TerminalBench) in sandboxed containers

The `readonly` profile enforces system-level restrictions for safe inspection of production systems. The `developer` profile unlocks file editing and shell access with configurable guardrails. The `eval` profile provides unrestricted access for testing the full capabilities of the harness in isolated environments.

## Installation

```bash
# Development (editable, in a virtualenv)
uv sync

# Global install (adds rho-agent and rho-eval to PATH)
uv tool install .
```

## Testing

```bash
# Install dev dependencies (pytest, pytest-asyncio, ruff)
uv sync --group dev

# Run all tests
uv run --group dev python -m pytest
```

## Running the Agent

### Interactive CLI

Start a REPL session for exploratory, multi-turn research:

```bash
uv run rho-agent main
uv run rho-agent main --profile developer --working-dir ~/proj/myapp
```

The interactive mode supports multi-line input (Esc+Enter), tab completion, session history, and in-session commands (`/approve`, `/compact`, `/help`, `/clear`, `exit`).

### Single Command (Dispatch)

Run a one-off task and exit—useful for scripting or CI:

```bash
# Inline prompt
uv run rho-agent main "what does this project do?"
uv run rho-agent main --output summary.md "summarize the error handling"

# From a prompt template (with variable substitution)
uv run rho-agent main --prompt examples/job-failure.md \
  --var cluster=prod --var log_path=/mnt/logs/12345
```

Prompt templates use YAML frontmatter and Jinja2 variables. See [Prompt Files](#prompt-files) for details.

### Programmatic API (First-Class)

For dispatching many agents from Python (services, workers, batch jobs), use
the package API in `rho_agent.runtime` instead of shelling out to CLI:

```python
import asyncio
from rho_agent import (
    RuntimeOptions,
    close_runtime,
    create_runtime,
    run_prompt,
    start_runtime,
)


async def main() -> None:
    runtime = create_runtime(
        "You are a research assistant.",
        options=RuntimeOptions(
            profile="developer",
            working_dir="/tmp/work",
            team_id="acme",
            project_id="incident-response",
            telemetry_metadata={"job_id": "job-123", "shard": "3"},
        ),
    )
    status = "completed"
    await start_runtime(runtime)
    try:
        result = await run_prompt(runtime, "Analyze recent failures and summarize root causes.")
        status = result.status
        print(result.text)
        print(result.status, result.usage)
    finally:
        await close_runtime(runtime, status)

asyncio.run(main())
```

`RuntimeOptions` supports the same observability fields as CLI (`team_id`,
`project_id`, `observability_config`) plus `telemetry_metadata` and
`session_id` to make high-volume dispatch traceable.

### Web UI

For a graphical interface, see the [SQL Explorer Demo](examples/sql_explorer/README.md)—a Streamlit app for chatting with an agent to explore a database and export queries and results:

```bash
python examples/sql_explorer/seed_database.py      # seed sample database
uv run streamlit run examples/sql_explorer/app.py   # launch app
```

See [`examples/`](examples/README.md) for more runtime API usage patterns.

## Examples

### Database Research (readonly)

```bash
$ SQLITE_DB=test_data.db uv run rho-agent main
╭──────────────────────────────────────────────────────────────────────────────╮
│ rho-agent - Research assistant                                                │
│ Profile: readonly | Model: gpt-5-mini                                        │
╰──────────────────────────────────────────────────────────────────────────────╯

> what tables are in the database?

sqlite(operation='list_tables')
  → 3 rows
    table_name | type
    -----------+------
    job_logs   | table
    jobs       | table
    users      | table

The database contains these tables: `job_logs`, `jobs`, `users`

> show me the failed jobs

sqlite(operation='query', sql='SELECT * FROM jobs WHERE status = 'failed'')
  → 2 rows
    id | name            | status | user_id | started_at          | exit_code
    ---+-----------------+--------+---------+---------------------+----------
    2  | ml_training_run | failed | 2       | 2024-01-15 09:00:00 | 1
    6  | etl_nightly     | failed | 2       | 2024-01-14 23:00:00 | 137

Here are the failed jobs:

1) id: 2 - `ml_training_run` - exit_code: 1
2) id: 6 - `etl_nightly` - exit_code: 137

> why did ml_training_run fail?

sqlite(operation='query', sql='SELECT * FROM job_logs WHERE job_id = 2')
  → 3 rows
    id | job_id | level | message
    ---+--------+-------+------------------------------------------------
    4  | 2      | INFO  | Training started with lr=0.001
    5  | 2      | WARN  | GPU memory usage at 95%
    6  | 2      | ERROR | CUDA out of memory. Tried to allocate 2.00 GiB

The `ml_training_run` job failed due to a GPU out-of-memory error.
Immediate cause: **CUDA out of memory when trying to allocate 2 GiB.**

> exit
```

### Multi-Tool Developer Workflow

In `developer` mode the agent can read, write, edit files, and run shell commands—chaining tools together to complete multi-step tasks. Here a single prompt triggers five tool calls across four tools (`read` → `write` → `bash` → `edit` → `bash`):

```bash
$ uv run rho-agent main --profile developer --working-dir ~/proj/myapp \
    "read config/settings.yaml and write a script to validate its values, \
     then run it. If any checks fail, fix the config and re-run."

read(path='config/settings.yaml', start_line=1, end_line=500)
  → Read lines 1-13 of 13
         1  database:
         2    host: localhost
         3    port: 5432
         4    name: myapp
         5    max_connections: -1
    ... (7 more lines)

write(path='validate_config.py', content='import sys\nfrom pathlib ...')
  → Created validate_config.py (1809 bytes, 57 lines)

bash(python validate_config.py)
  → Validation failed:
     - database.max_connections must be a positive integer, got -1

edit(path='config/settings.yaml',
     old_string='max_connections: -1',
     new_string='max_connections: 100')
  → Applied

bash(python validate_config.py)
  → Validation passed

Done. The config had `max_connections: -1` which is invalid — updated
it to `100` and all validation checks now pass. The validation script is
at `validate_config.py`.
```

## Features

- **Capability profiles**: Three built-in profiles (readonly, developer, eval) plus custom YAML profiles
- **Fine-grained controls**: Shell command allowlisting, per-tool approval requirements, SQL mutation restrictions
- **File editing**: Write and edit tools available in developer/eval profiles
- **Multiple database backends**: SQLite, PostgreSQL, MySQL, Oracle, Vertica—with configurable read-only or mutation access
- **Prompt templates**: Markdown files with variable substitution for repeatable investigations
- **Observability**: Session tracking, token usage, tool execution metrics with Streamlit dashboard
- **Session management**: List and kill running agents from another terminal with `rho-agent ps` and `rho-agent kill`
- **Evaluation integrations**: BIRD-Bench and Harbor/TerminalBench

## Capability Profiles

The agent's capabilities are controlled via profiles:

| Profile | Shell | File Write | Database | Use Case |
|---------|-------|------------|----------|----------|
| `readonly` | Restricted (allowlist) | Off | SELECT only | Safe research on production systems |
| `developer` | Unrestricted | Full | SELECT only | Local development with file editing |
| `eval` | Unrestricted | Full | Full | Sandboxed benchmark execution |

```bash
# Use a specific profile
uv run rho-agent main --profile readonly
uv run rho-agent main --profile developer
uv run rho-agent main --profile eval

# Custom YAML profile
uv run rho-agent main --profile ~/.config/rho-agent/profiles/my-profile.yaml
```

## Prompt Files

Prompt files are markdown documents with optional YAML frontmatter. The markdown body becomes the system prompt:

```markdown
---
description: Investigate a failed job
variables:
  cluster: { required: true }
  log_path: { required: true }
  job_id: { default: "unknown" }
initial_prompt: Investigate job {{ job_id }} on {{ cluster }}.
---

You are debugging a failed job on {{ cluster }}.

Log location: {{ log_path }}

## Strategy
1. Search for ERROR, FATAL, Exception
2. Find the earliest failure (not cascading errors)
3. Map error to code location
```

### Prompt Precedence

1. `--system "..."` — override system prompt entirely
2. `--prompt file.md` — load markdown file
3. `~/.config/rho-agent/default-system.md` — custom default (if exists)
4. Built-in default

### Initial Message

1. Positional argument (`rho-agent main --prompt x.md "focus on OOM"`)
2. Frontmatter `initial_prompt`
3. Neither → interactive mode

## Tools

### Core Tools (always available)

| Tool | Purpose |
|------|---------|
| `grep` | Regex search with ripgrep |
| `read` | Read file contents with optional line ranges |
| `list` | Explore directories (flat or recursive tree) |
| `glob` | Find files by glob pattern |
| `read_excel` | Read Excel files (list sheets, read data, get info) |

### Capability-Dependent Tools

| Tool | Availability | Purpose |
|------|-------------|---------|
| `bash` | Always (restricted or unrestricted based on profile) | Run shell commands |
| `write` | `developer`, `eval` profiles | Create new files (or overwrite in FULL mode) |
| `edit` | `developer`, `eval` profiles (FULL mode) | Surgical file edits via search-and-replace |

### Database Tools

Available when configured via environment variables:

| Tool | Enable With |
|------|-------------|
| `sqlite` | `SQLITE_DB` |
| `postgres` | `POSTGRES_HOST`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `mysql` | `MYSQL_HOST`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` |
| `oracle` | `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` |
| `vertica` | `VERTICA_HOST`, `VERTICA_DATABASE`, `VERTICA_USER`, `VERTICA_PASSWORD` |

Each supports `list_tables`, `describe`, `query`, and `export_query`. Query restrictions depend on the profile—`readonly` and `developer` enforce SELECT-only, while `eval` allows mutations.

## Configuration

```bash
# .env
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=http://localhost:8000/v1  # optional
OPENAI_MODEL=gpt-5-mini                        # optional
```

### Conversations

Sessions auto-save to `~/.config/rho-agent/conversations/`:

```bash
uv run rho-agent main --list           # list saved
uv run rho-agent main --resume latest  # resume most recent
uv run rho-agent main -r <id>          # resume by ID
```

## Commands

In interactive mode:

| Command | Description |
|---------|-------------|
| `/approve` | Auto-approve all tool calls |
| `/compact` | Compress conversation history |
| `/help` | Show help |
| `/clear` | Clear screen |
| `exit` | Quit |

## Session Management

List and kill running agent sessions from a separate terminal. Useful when you have multiple agents running across terminal tabs and want to stop specific ones without hunting for the right window.

```bash
# List running agents
uv run rho-agent ps
#   a1b2c3d4  running  gpt-5-mini      45s  analyze the logs in /var/log/app...
#   e5f6g7h8  running  gpt-5-mini      32s  find all database schemas in...

# Kill one by session ID prefix
uv run rho-agent kill a1b2

# Kill all running agents
uv run rho-agent kill --all

# Clean up stale entries from crashed agents
uv run rho-agent ps --cleanup
```

Killed agents exit with `cancelled` status in telemetry, with `cancel_source: "kill_command"` in session metadata to distinguish from Ctrl+C cancellations.

The signal protocol uses files in `~/.config/rho-agent/signals/` (override with `RHO_AGENT_SIGNAL_DIR`). Each agent writes a `.running` file on start and removes it on exit. The `kill` command writes a `.cancel` file that the agent detects between tool calls.

## Observability

Track agent sessions, token usage, and tool executions with the built-in observability system.

### Enabling Telemetry

Both `--team-id` and `--project-id` are required to enable telemetry — if either is missing, no data is recorded.

```bash
# Via CLI flags
uv run rho-agent main --team-id acme --project-id logs "analyze this error"

# Via environment variables
export RHO_AGENT_TEAM_ID=acme
export RHO_AGENT_PROJECT_ID=logs
uv run rho-agent main "analyze this error"
```

### Dashboard

Launch the Streamlit dashboard to view session history and analytics:

```bash
uv run rho-agent dashboard
uv run rho-agent dashboard --port 8502  # custom port
```

The dashboard shows:
- Session history with status, tokens, and tool calls
- Session detail view with turn-by-turn breakdown
- Token usage analytics by team/project
- Tool execution statistics

### What's Tracked

| Metric | Description |
|--------|-------------|
| Sessions | Start/end time, status, model, team/project |
| Turns | Per-turn token counts (input/output) |
| Tool executions | Tool name, arguments, success/failure, duration |

Data is stored in SQLite at `~/.config/rho-agent/telemetry.db` by default.

### Configuration File

For advanced configuration, create `~/.config/rho-agent/observability.yaml`:

```yaml
observability:
  enabled: true
  tenant:
    team_id: acme
    project_id: logs
  backend:
    type: sqlite
    sqlite:
      path: ~/.config/rho-agent/telemetry.db
  capture:
    traces: true
    metrics: true
    tool_arguments: true
    tool_results: false  # can be large
```

## CLI Reference

```
uv run rho-agent main [PROMPT] [OPTIONS]

Options:
  -p, --prompt FILE      Markdown prompt file
  --var KEY=VALUE        Variable for prompt (repeatable)
  --vars-file FILE       YAML file with variables
  -s, --system TEXT      Override system prompt
  -o, --output FILE      Write response to file
  -w, --working-dir DIR  Working directory
  -m, --model MODEL      Model to use
  --base-url URL         API endpoint
  --profile NAME         Capability profile (readonly, developer, eval, or path)
  -y, --auto-approve     Skip tool approval prompts
  -r, --resume ID        Resume conversation
  -l, --list             List saved conversations
  --team-id ID           Team ID for observability
  --project-id ID        Project ID for observability

# Launch observability dashboard
uv run rho-agent dashboard [--port PORT] [--db PATH]

# List running agent sessions
uv run rho-agent ps [--cleanup]

# Kill running agent sessions
uv run rho-agent kill [PREFIX] [--all]
```

## Evaluations

rho-agent includes integrations for running LLM benchmarks:

### BIRD-Bench

```bash
# Text-to-SQL benchmark
rho-eval bird ~/proj/bird-bench-mini-dev/mini_dev_data/mini_dev_sqlite.json \
  ~/proj/bird-bench-mini-dev/mini_dev_data/dev_databases/
```

See `rho_agent/eval/birdbench/README.md` for setup and options.

### Harbor / TerminalBench

```bash
cd ~/proj/harbor
uv run harbor run --config ~/proj/rho-agent/rho_agent/eval/harbor/configs/terminal-bench-sample.yaml
```

See `rho_agent/eval/harbor/README.md` for details.
