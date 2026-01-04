# ro-agent

A read-only research agent for searching directories, inspecting files, and exploring code or databases—without modifying anything.

## Installation

```bash
uv sync
```

## Usage

```bash
# Interactive mode
uv run ro-agent

# Single prompt (agent completes task and exits)
uv run ro-agent "what does this project do?"

# With working directory context
uv run ro-agent --working-dir ~/proj/myapp "find the error handling code"

# Auto-approve shell commands
uv run ro-agent --auto-approve "inspect the logs"

# Custom model/endpoint
uv run ro-agent --base-url http://localhost:8000/v1 --model qwen2.5-72b

# Save output to file
uv run ro-agent --output ./summary.md "summarize this project"

# Template-based dispatch (see Templates section)
uv run ro-agent --template job-failure --var cluster=prod --var log_path=/mnt/logs/12345
```

## Templates

Templates enable dispatch mode—launching the agent with pre-configured prompts and context for specific tasks like investigating job failures.

### Usage

```bash
# Run with a template
uv run ro-agent --template job-failure \
  --var cluster=prod-gpu \
  --var log_path=/mnt/logs/job-12345

# Variables from file
uv run ro-agent --template job-failure --vars-file ./job-context.yaml

# Capture output to file
uv run ro-agent --template job-failure \
  --var log_path=/mnt/logs/12345 \
  --output findings.txt
```

### Template Format

Templates live in `~/.config/ro-agent/templates/`. Each template defines variables, a system prompt, and an optional initial prompt:

```yaml
# ~/.config/ro-agent/templates/job-failure.yaml
name: job-failure
description: Investigate a failed distributed job

variables:
  cluster:
    description: Cluster name
    required: true
  log_path:
    description: Path to log directory
    required: true
  job_id:
    required: false
    default: "unknown"

# Reference a layout file for repo context
repo_layout: "ml-training"

system_prompt: |
  You are investigating a failed job on {{ cluster }}.
  Log location: {{ log_path }}

  ## Repository Layout
  {{ repo_layout }}

  Find the root cause and recommend a fix.

initial_prompt: |
  Job {{ job_id }} has failed. Investigate the logs at {{ log_path }}.
```

### Layouts

Layouts provide reusable repo/cluster context. They live in `~/.config/ro-agent/layouts/`:

```yaml
# ~/.config/ro-agent/layouts/ml-training.yaml
name: ml-training

structure: |
  ml-training/
  ├── scripts/       # Job entrypoints
  ├── src/models/    # Model architectures
  ├── src/data/      # Data loaders
  └── configs/       # Training configs

key_paths:
  entrypoints: scripts/
  models: src/models/

error_patterns:
  - pattern: "CUDA out of memory"
    likely_cause: "Batch size too large"
    look_in: ["configs/", "src/data/"]

cluster_context: |
  - Logs at /mnt/logs/{job_id}/
  - GPU nodes: 8x A100 80GB
```

The layout's `structure`, `key_paths`, `error_patterns`, and `cluster_context` are formatted and injected into `{{ repo_layout }}` in the template.

## Interactive Example

```
╭──────────────────────────────────────────────────────────────────╮
│ ro-agent - Read-only research assistant                          │
│ Model: gpt-4o                                                    │
│ Enter to send, Esc then Enter for newline.                       │
│ Type /help for commands, exit to quit.                           │
╰──────────────────────────────────────────────────────────────────╯

> What's in ~/proj/myapp?

list_dir(path='/home/user/proj/myapp', show_hidden=False)
  → 5 items

The directory contains:
- src/ and tests/ directories
- main.py (6.7KB) - likely the entry point
- README.md and pyproject.toml for project config

> Find files with "error" in them

grep_files(pattern='error', path='/home/user/proj/myapp', glob='*.py')
  → 2 files match

Found matches in:
- src/api.py
- src/handlers.py

> Show me the error handling in api.py

grep_files(pattern='error', path='/home/user/proj/myapp/src/api.py', output_mode='content')
  → 1 matches in 1 files

The error handling in api.py (line 43) catches exceptions from client
requests and logs a warning before returning None:

    try:
        response = self.client.request(endpoint)
    except Exception as error:
        logger.warning(f"Request failed: {error}")
        return None

> Write a summary to /tmp/findings.md

write_output(path='/tmp/findings.md', content='# Error Handling Summary...')
  → Wrote 256 bytes (12 lines)

Done! Summary written to /tmp/findings.md.

[2847 in, 892 out]

> exit
```

## Tools

Five built-in tools, modeled after Claude Code's patterns:

### `list_dir`
Explore directory structures with flat or recursive tree views.
```
list_dir(path="/data/logs")                           # flat listing
list_dir(path="/project", recursive=true, max_depth=3) # tree view
list_dir(path="/project", show_hidden=true)           # include dotfiles
```

### `grep_files`
Search for patterns across directory trees. Three output modes to control context usage:

| Mode | Description | Use Case |
|------|-------------|----------|
| `files_with_matches` (default) | Returns only file paths | Discover which files match |
| `content` | Returns matching lines with context | See actual matches |
| `count` | Returns match counts per file | Gauge match distribution |

```
# Find all Python files containing "TODO"
grep_files(pattern="TODO", path="/project/src", glob="*.py")

# Search logs for errors, see surrounding context
grep_files(pattern="ERROR|FATAL", path="/var/log", glob="*.log",
           output_mode="content", context_lines=3)

# Count matches per file
grep_files(pattern="import", path="/project", glob="*.py", output_mode="count")
```

### `read_file`
Read file contents with optional line ranges.
```
read_file(path="/path/to/file.py")                    # full file (up to 500 lines)
read_file(path="/path/to/file.py", start_line=100, end_line=200)  # specific range
```

### `shell`
Execute shell commands (requires approval). Allowlisted to safe read-only commands.
```
shell(command="jq '.errors' /data/results.json")
shell(command="wc -l *.py")
```

### `write_output`
Write content to a file (requires approval). Use this to export research findings.
```
write_output(path="/tmp/summary.md", content="# Summary\n...")
```

## Database Handlers

Read-only database inspection for Oracle, SQLite, and Vertica. Each handler exposes three operations through a single tool interface—keeping context overhead minimal while providing full schema exploration.

### Installation

```bash
uv add oracledb          # Oracle
uv add vertica-python    # Vertica
# sqlite3 is in stdlib
```

### Configuration

Set connection details via environment variables:

```bash
# Oracle
export ORACLE_DSN="host:port/service_name"
export ORACLE_USER="readonly_user"
export ORACLE_PASSWORD="..."

# Vertica
export VERTICA_HOST="vertica.example.com"
export VERTICA_PORT="5433"
export VERTICA_DATABASE="analytics"
export VERTICA_USER="readonly_user"
export VERTICA_PASSWORD="..."

# SQLite
export SQLITE_DB="/path/to/database.db"
```

Database handlers are only registered when their respective env vars are set.

### Operations

All three handlers (`oracle`, `sqlite`, `vertica`) support the same operations:

| Operation | Description | Key Parameters |
|-----------|-------------|----------------|
| `list_tables` | Find tables by pattern | `table_pattern` (% wildcards), `schema` |
| `describe` | Get table schema details | `table_name`, `schema` |
| `query` | Run read-only SQL | `sql`, `row_limit` |

### Examples

```
# List all tables starting with "CUSTOMER"
oracle(operation="list_tables", table_pattern="CUSTOMER%")

# Describe a specific table
oracle(operation="describe", table_name="orders", schema="sales")

# Run a query (mutations are blocked)
sqlite(operation="query", sql="SELECT * FROM users LIMIT 10")

# Query with row limit
vertica(operation="query", sql="SELECT * FROM events", row_limit=50)
```

### Safety

- **Read-only enforcement**: SQL is validated to block INSERT, UPDATE, DELETE, DROP, etc.
- **Connection-level protection**: SQLite opens with `?mode=ro`, Vertica uses `read_only=True`
- **Row limits**: Default 100 rows per query to prevent context overflow
- **Requires approval**: All database operations require user confirmation

### Architecture

The handlers share a common base class (`DatabaseHandler`) that provides:
- SQL mutation detection and blocking
- Result formatting as ASCII tables
- Consistent tool schema and operation dispatch

Each subclass implements only the database-specific parts:
- Connection handling
- System catalog queries (e.g., `USER_TABLES` vs `sqlite_master` vs `V_CATALOG`)
- Extra metadata fetching (primary keys, indexes)

```
ro_agent/tools/handlers/
├── database.py   # Base class with shared logic
├── oracle.py     # Oracle-specific catalog queries
├── sqlite.py     # SQLite pragma-based introspection
└── vertica.py    # Vertica V_CATALOG queries
```

## Safety

- **Dedicated read-only tools**: `read_file`, `list_dir`, `grep_files` run without approval
- **Shell allowlist**: Only safe commands allowed (grep, cat, jq, etc.)
- **Dangerous pattern blocking**: Rejects rm, sudo, redirects, etc.
- **Approval prompts**: Shell commands require confirmation (use `--auto-approve` to skip)
- **Output truncation**: Large outputs are truncated to prevent context overflow

## Configuration

Create a `.env` file:

```bash
OPENAI_API_KEY=your-key-here
OPENAI_BASE_URL=http://your-vllm-server:8000/v1  # optional
OPENAI_MODEL=gpt-4o  # optional
```

History is stored at `~/.config/ro-agent/history`.

## Architecture

```
ro_agent/
├── cli.py              # Entry point, REPL, event handling
├── core/
│   ├── agent.py        # Main agent loop (prompt → model → tools → loop)
│   └── session.py      # Conversation history management
├── client/
│   └── model.py        # OpenAI-compatible streaming client
└── tools/
    ├── base.py         # ToolHandler ABC
    ├── registry.py     # Tool registration and dispatch
    └── handlers/
        ├── read_file.py
        ├── list_dir.py
        ├── grep_files.py
        ├── shell.py
        ├── write_output.py  # Export findings to files
        ├── database.py      # Base class for DB handlers
        ├── oracle.py        # Oracle handler
        ├── sqlite.py        # SQLite handler
        └── vertica.py       # Vertica handler
```

Based on [Codex CLI](https://github.com/openai/codex) architecture patterns.

## Adding Tools

Implement `ToolHandler` and register in `cli.py`:

```python
from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput

class MyHandler(ToolHandler):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "What this tool does"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "..."},
            },
            "required": ["arg1"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # True for potentially dangerous tools

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        # Do the work
        return ToolOutput(content="result", success=True)
```

## Interactive Commands

| Command | Description |
|---------|-------------|
| `/approve` | Enable auto-approve for session |
| `/compact [guidance]` | Compact conversation history (see below) |
| `/help` | Show help |
| `/clear` | Clear screen |
| `exit` | Quit |

## Context Management

ro-agent includes compaction features to manage long conversations:

### Manual Compaction
Use `/compact` to summarize the conversation when context gets long:
```
> /compact
Compacting conversation...
Compacted: 45000 → 3200 tokens

> /compact focus on the database schema findings
Compacting conversation...
Compacted: 32000 → 2800 tokens
```

### Auto-Compaction
When context approaches 80% of the limit (default 100k tokens), ro-agent automatically compacts before processing your next message:
```
Context limit approaching, auto-compacting...
Compacted: 82000 → 4500 tokens
```

The compaction creates a handoff summary that preserves:
- Progress and key decisions made
- Important context and user preferences
- Next steps and remaining work
- Critical file paths and references
