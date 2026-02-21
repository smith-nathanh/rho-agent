---
title: Tools
description: Complete reference for all tool handlers available to agents.
order: 7
---

Tools follow a handler pattern where each tool defines a `name`, `description`, JSON-schema `parameters`, and a `handle()` implementation. Tool availability is controlled by the active [permission profile](profiles/).

## File inspection tools

These tools are always available regardless of profile.

### `read`

Read file contents with optional line ranges.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path (required) |
| `start_line` | integer | First line to read |
| `end_line` | integer | Last line to read |

Returns line-numbered content. Blocks binary files. Defaults to 500 lines max, truncating lines longer than 500 characters.

### `grep`

Search file contents using ripgrep.

| Parameter | Type | Description |
|---|---|---|
| `pattern` | string | Search pattern (required) |
| `path` | string | File or directory to search |
| `glob` | string | File glob filter |
| `ignore_case` | boolean | Case-insensitive matching |
| `context_lines` | integer | Lines of context around matches |
| `max_matches` | integer | Maximum results (default: 100) |

Automatically skips `.git`, `node_modules`, `__pycache__`, and `.venv` directories.

### `glob`

Find files by glob pattern.

| Parameter | Type | Description |
|---|---|---|
| `pattern` | string | Glob pattern (required) |
| `path` | string | Base directory |
| `max_results` | integer | Maximum results (default: 100) |

### `list`

List directory contents as a flat listing or recursive tree.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | Directory path (required) |
| `show_hidden` | boolean | Include hidden files |
| `recursive` | boolean | Recursive tree view |
| `max_depth` | integer | Maximum tree depth |

Flat mode shows permissions, size, mtime, and name. Recursive mode shows a tree structure. Defaults to 200 entries max.

### `read_excel`

Read and inspect Excel files (.xlsx, .xls).

| Parameter | Type | Description |
|---|---|---|
| `path` | string | Excel file path (required) |
| `action` | string | `list_sheets`, `read_sheet`, or `get_info` |
| `sheet` | string | Sheet name or index |
| `start_row` | integer | First row to read |
| `end_row` | integer | Last row to read |
| `show_hidden` | boolean | Include hidden rows/columns |

Returns tab-delimited data or sheet metadata. Defaults to 500 rows max.

## Shell tool

### `bash`

Execute shell commands. Behavior depends on the profile's shell mode.

| Parameter | Type | Description |
|---|---|---|
| `command` | string | Shell command to execute (required) |
| `working_dir` | string | Working directory override |
| `timeout` | integer | Timeout in seconds |

**Restricted mode** (`readonly` profile): Only allowlisted commands are permitted. The allowlist includes read-only commands like `cat`, `grep`, `find`, `ls`, `head`, `tail`, `wc`, `jq`, `git log`, `git diff`, `git show`, `ps`, `df`, `du`, `env`, `curl`, and similar inspection tools. Redirects (`>`, `>>`) and destructive commands (`rm`, `mv`, `chmod`, `kill`, `sudo`) are blocked.

**Unrestricted mode** (`developer` and `eval` profiles): Any command is allowed.

Returns JSON with `output`, `exit_code`, and `duration_seconds`.

## File edit tools

Available when the profile's `file_write` mode is `create-only` or `full`.

### `write`

Create or overwrite files.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path (required) |
| `content` | string | File content (required) |

**Create-only mode**: Can only create new files. Blocks overwrites and writes to sensitive paths (`.bashrc`, `.ssh/`, `.aws/`, `/etc/`, `/usr/`).

**Full mode**: Unrestricted write access.

### `edit`

Surgical file edits via search-and-replace.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path (required) |
| `old_string` | string | Text to find (required) |
| `new_string` | string | Replacement text (required) |

Requires a unique match to prevent accidental changes. Uses a three-stage matching strategy:

1. **Exact match** — literal string comparison
2. **Whitespace-normalized** — matches ignoring whitespace differences
3. **Indentation-flexible** — matches with different indentation while preserving relative indentation in the replacement

## Database tools

Database tools share a common interface. All support `query`, `list_tables`, `describe`, and `export_query` operations. By default, only SELECT queries are allowed. Mutation queries (INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE) require the `eval` profile.

| Parameter | Type | Description |
|---|---|---|
| `database` | string | Database alias (for multi-database configs) |
| `operation` | string | `query`, `list_tables`, `describe`, or `export_query` |
| `sql` | string | SQL query (for `query` and `export_query`) |
| `table_pattern` | string | Filter pattern for `list_tables` |
| `table_name` | string | Table name for `describe` |
| `output_path` | string | CSV output path for `export_query` |

Query results are formatted as ASCII tables with a default limit of 100 rows.

### `sqlite`

Configured via `SQLITE_DB` environment variable. Supports multiple databases as a comma-separated list.

### `postgres`

Configured via `POSTGRES_HOST`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.

### `mysql`

Configured via `MYSQL_HOST`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`.

### `oracle`

Configured via `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`.

### `vertica`

Configured via `VERTICA_HOST`, `VERTICA_DATABASE`, `VERTICA_USER`, `VERTICA_PASSWORD`.

## Daytona remote sandbox tools

When the `daytona` profile is active, all file and shell tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`) are replaced with remote equivalents that execute in a Daytona cloud VM. The tool names and parameter schemas are identical — the model sees the same interface, but execution happens remotely.

A `SandboxManager` lazily provisions a sandbox on the first tool call and tears it down when the session closes. All handlers share the same sandbox instance.

### How it works

1. Agent dispatches a tool call (e.g., `bash` with `command: "ls -la"`)
2. The Daytona handler forwards the command to the remote sandbox via the Daytona SDK
3. Output is returned to the agent in the same format as local handlers

### Configuration

| Environment variable | Default | Description |
|---|---|---|
| `DAYTONA_API_KEY` | — | API key for Daytona (required) |
| `DAYTONA_API_URL` | Daytona default | API endpoint override |
| `DAYTONA_SANDBOX_IMAGE` | `ubuntu:latest` | Container image for the sandbox |
| `DAYTONA_SANDBOX_CPU` | — | CPU cores |
| `DAYTONA_SANDBOX_MEMORY` | — | Memory in MB |
| `DAYTONA_SANDBOX_DISK` | — | Disk in GB |

Database tools continue to run locally even under the `daytona` profile.

## Sub-agent tools

rho-agent supports two patterns for spawning child agents. Both create independent sessions and disable further delegation to prevent unbounded recursion.

### `delegate` (ad-hoc delegation)

Spawn a child agent to execute a focused subtask. The LLM decides at runtime when to delegate. See [Architecture](architecture/) for details on multi-agent coordination.

| Parameter | Type | Description |
|---|---|---|
| `instruction` | string | Task description for the child agent (required) |
| `full_context` | boolean | Copy parent conversation history to child |

The child agent inherits the parent's profile, model, and working directory. Delegation is single-level — child agents cannot delegate further. Parent cancellation propagates to children automatically.

### Agent-as-tool (pre-configured specialists)

Wrap a pre-configured agent as a named tool with typed parameters. Unlike `delegate`, the child has its own system prompt, profile, and model — configured at setup time, not derived from the parent.

```python
from rho_agent.tools.handlers import AgentToolHandler
from rho_agent.core.config import AgentConfig

sql_agent = AgentToolHandler(
    tool_name="generate_sql",
    tool_description="Generate SQL from a natural language question.",
    system_prompt="You are an expert SQL developer. ...",
    config=AgentConfig(model="gpt-5-mini", profile="readonly"),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Natural language question"},
            "dialect": {"type": "string", "enum": ["sqlite", "postgres", "mysql"]},
        },
        "required": ["question"],
    },
)

# Register on an existing agent's registry
agent.registry.register(sql_agent)
```

The parent LLM sees `generate_sql(question, dialect)` as a first-class tool. Each invocation spawns an independent session — no conversation history is shared.

| Constructor parameter | Type | Description |
|---|---|---|
| `tool_name` | string | Tool name the LLM sees (required) |
| `tool_description` | string | Description for the LLM (required) |
| `system_prompt` | string | Child agent's system prompt (required) |
| `config` | AgentConfig | Child's model, profile, etc. |
| `input_schema` | dict | JSON Schema for typed parameters |
| `input_formatter` | callable | Custom `(args) -> instruction` mapper |
| `requires_approval` | bool | Whether parent needs approval before calling |
