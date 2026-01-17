# ro-agent

A read-only agent for inspecting logs, files, and databases—without modifying anything.

## Installation

```bash
uv sync
```

## Usage Modes

### Interactive Mode

Start a REPL session for exploratory research:

```bash
uv run ro-agent
uv run ro-agent --working-dir ~/proj/myapp
```

### Dispatch Mode

Run with a prompt file for repeatable, task-specific investigations:

```bash
uv run ro-agent --prompt examples/job-failure.md \
  --var cluster=prod --var log_path=/mnt/logs/12345
```

The agent uses the prompt file as its system prompt, substitutes variables, and runs the `initial_prompt` from frontmatter automatically.

### Single Prompt Mode

Run a one-off query and exit:

```bash
uv run ro-agent "what does this project do?"
uv run ro-agent --output summary.md "summarize the error handling"
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
3. `~/.config/ro-agent/default-system.md` — custom default (if exists)
4. Built-in default

### Initial Message

1. Positional argument (`ro-agent --prompt x.md "focus on OOM"`)
2. Frontmatter `initial_prompt`
3. Neither → interactive mode

## Tools

The agent has read-only tools for research:

| Tool | Purpose |
|------|---------|
| `search` | Regex search with ripgrep (fast, streams results) |
| `read_file` | Read file contents with optional line ranges |
| `list_dir` | Explore directories (flat or recursive tree) |
| `find_files` | Find files by glob pattern |
| `shell` | Run shell commands (allowlisted, requires approval) |
| `read_excel` | Read Excel files (list sheets, read data, get info) |
| `write_output` | Export findings to a new file |

### Database Tools

Available when configured via environment variables:

| Tool | Enable With |
|------|-------------|
| `oracle` | `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` |
| `sqlite` | `SQLITE_DB` |
| `vertica` | `VERTICA_HOST`, `VERTICA_PORT`, `VERTICA_DATABASE`, `VERTICA_USER`, `VERTICA_PASSWORD` |

Each supports `list_tables`, `describe`, `query`, and `export_query` (export results to CSV). All queries are read-only enforced.

## Configuration

```bash
# .env
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=http://localhost:8000/v1  # optional
OPENAI_MODEL=gpt-4o                        # optional
```

### Conversations

Sessions auto-save to `~/.config/ro-agent/conversations/`:

```bash
uv run ro-agent --list           # list saved
uv run ro-agent --resume latest  # resume most recent
uv run ro-agent -r <id>          # resume by ID
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

## Example Session

```bash
$ SQLITE_DB=test_data.db uv run ro-agent
╭──────────────────────────────────────────────────────────────────────────────╮
│ ro-agent - Read-only research assistant                                      │
│ Model: gpt-5.1                                                               │
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

## CLI Reference

```
uv run ro-agent [PROMPT] [OPTIONS]

Options:
  -p, --prompt FILE      Markdown prompt file
  --var KEY=VALUE        Variable for prompt (repeatable)
  --vars-file FILE       YAML file with variables
  -s, --system TEXT      Override system prompt
  -o, --output FILE      Write response to file
  -w, --working-dir DIR  Working directory
  -m, --model MODEL      Model to use
  --base-url URL         API endpoint
  -y, --auto-approve     Skip tool approval prompts
  -r, --resume ID        Resume conversation
  -l, --list             List saved conversations
```
