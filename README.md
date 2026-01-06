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

search(pattern='error', path='/home/user/proj/myapp', glob='*.py')
  → 12 matches

Found matches in:
- src/api.py
- src/handlers.py

> Show me the error handling in api.py

search(pattern='error', path='/home/user/proj/myapp/src/api.py')
  → 3 matches

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

### `search`
Search for patterns in files using ripgrep. Efficient for large log files—never loads files into memory.

```
# Find all Python files containing "TODO"
search(pattern="TODO", path="/project/src", glob="*.py")

# Search logs for errors, see surrounding context
search(pattern="ERROR|FATAL", path="/var/log", glob="*.log", context_lines=3)

# Case-insensitive search with match limit
search(pattern="error", path="/var/log", ignore_case=true, max_matches=50)
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

- **Dedicated read-only tools**: `read_file`, `list_dir`, `search` run without approval
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
        ├── search.py
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

## Programmatic Usage

You can embed ro-agent in Python scripts to run it programmatically—either for simple queries or autonomous multi-turn exploration with tools.

### Basic Example

```python
import asyncio
from dotenv import load_dotenv

load_dotenv()  # Load OPENAI_API_KEY from .env

from ro_agent.core.agent import Agent
from ro_agent.core.session import Session
from ro_agent.client.model import ModelClient
from ro_agent.tools.registry import ToolRegistry
from ro_agent.tools.handlers import (
    ReadFileHandler,
    SearchHandler,
    FindFilesHandler,
    ListDirHandler,
)


async def run_agent(task: str, working_dir: str) -> str:
    """Run agent with tools, let it explore autonomously."""

    session = Session(
        system_prompt=f"You are a research assistant. Working directory: {working_dir}"
    )

    # Register tools
    registry = ToolRegistry()
    registry.register(ReadFileHandler())
    registry.register(SearchHandler())
    registry.register(FindFilesHandler())
    registry.register(ListDirHandler())

    # Create client and agent
    client = ModelClient(model="gpt-4o")

    # Auto-approve all tool calls (or implement custom logic)
    async def auto_approve(tool_name: str, tool_args: dict) -> bool:
        return True

    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=auto_approve,
    )

    # Run and collect response
    response = ""
    async for event in agent.run_turn(task):
        if event.type == "text" and event.content:
            response += event.content
            print(event.content, end="", flush=True)  # Stream to console
        elif event.type == "tool_start":
            print(f"\n[{event.tool_name}]")
        elif event.type == "turn_complete":
            print(f"\n[Done: {event.usage}]")

    return response


# Usage
result = asyncio.run(run_agent(
    task="What does this project do?",
    working_dir="/path/to/project"
))
```

### Key Concepts

| Component | Purpose |
|-----------|---------|
| `Session` | Manages conversation history and system prompt |
| `ToolRegistry` | Holds tool handlers, dispatches invocations |
| `ModelClient` | OpenAI-compatible API client (streaming) |
| `Agent` | Orchestrates the turn loop (prompt → model → tools → loop) |
| `AgentEvent` | Events yielded during execution (`text`, `tool_start`, `tool_end`, `turn_complete`, `error`) |

### Event Types

The `run_turn()` method yields `AgentEvent` objects:

- `text` - Streaming text from the model (access via `event.content`)
- `tool_start` - Tool invocation started (`event.tool_name`, `event.tool_args`)
- `tool_end` - Tool completed (`event.tool_result`, `event.tool_metadata`)
- `turn_complete` - Turn finished (`event.usage` has token counts)
- `error` - Error occurred (`event.content`)

### Approval Callback

Control which tools require approval:

```python
async def my_approval(tool_name: str, tool_args: dict) -> bool:
    # Auto-approve read-only tools
    if tool_name in ("read_file", "search", "list_dir", "find_files"):
        return True
    # Reject shell commands with certain patterns
    if tool_name == "shell" and "rm" in tool_args.get("command", ""):
        return False
    # Prompt user for others
    return input(f"Approve {tool_name}? [y/N] ").lower() == "y"
```

### Simple Query (No Tools)

For simple context + question scenarios without tool use:

```python
async def simple_query(context: str, question: str) -> str:
    session = Session(system_prompt=f"Context:\n{context}")
    registry = ToolRegistry()  # No tools
    client = ModelClient(model="gpt-4o")
    agent = Agent(session=session, registry=registry, client=client)

    response = ""
    async for event in agent.run_turn(question):
        if event.type == "text":
            response += event.content or ""
    return response

# Usage
answer = asyncio.run(simple_query(
    context="Error: Connection refused at line 42",
    question="What caused this error?"
))
```

See `examples/programmatic_usage.py` for a complete working example.

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
