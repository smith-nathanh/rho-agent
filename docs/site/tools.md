---
title: Tools
description: Complete reference for all tool handlers available to agents.
order: 8
---

Tools follow a handler pattern where each tool defines a `name`, `description`, JSON-schema `parameters`, and a `handle()` implementation. Which tools an agent gets follows from its [access level](access/) and, optionally, `AgentConfig.tools`, a list of tool names.

## File inspection tools

These tools are available at every access level. In a sandbox they refuse protected paths such as `~/.ssh`.

### `read`

Read file contents with optional line ranges.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path (required) |
| `start_line` | integer | First line to read |
| `end_line` | integer | Last line to read |

Returns line-numbered content. Blocks binary files. Defaults to 500 lines max, truncating lines longer than 500 characters. An image file is attached for the model to look at, the same as `view_image`.

### `view_image`

Attach an image file (PNG, JPEG, GIF or WebP) so the model can see it: a screenshot, a plot, a camera frame.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | Image path (required) |

The image is added to the conversation right after the step's tool results. Files over 4 MB are scaled down when Pillow is installed and refused otherwise. The model must accept image input.

`view_image` is on by default. Leave it out of `AgentConfig.tools` for a model without image input; `read` then stops attaching images too. `tools=["bash", "view_image"]` gives exactly two tools, bash and images.

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

Execute shell commands. Any command is allowed; with `read-only` or `workspace` access it runs inside the OS sandbox, which refuses writes outside the allowed directories, reads of credentials, and connections to local services such as Docker (see [Access](access/#how-it-works)).

| Parameter | Type | Description |
|---|---|---|
| `command` | string | Shell command to execute (required) |
| `working_dir` | string | Working directory override |
| `timeout` | integer | Timeout in seconds (default 300) |
| `run_in_background` | boolean | Start the command and return at once with its PID and log file |
| `unsandboxed` | boolean | Sandboxed agents only: run this one command outside the sandbox, after the user approves |
| `reason` | string | With `unsandboxed`: why, shown to the user |

Returns JSON with `output`, `exit_code`, and `duration_seconds`.

### `request_access`

Ask the user for access outside the sandbox. Offered only to sandboxed agents, and only when someone can answer (see [Access](access/#asking-for-more)).

| Parameter | Type | Description |
|---|---|---|
| `path` | string | Directory to write, or protected path to read (required) |
| `access` | string | `write` (default) or `read` |
| `reason` | string | One sentence shown to the user (required) |

A yes lasts for the rest of the session and applies to every tool and command.

## File edit tools

Available with `workspace` and `full` access. In a sandbox, a path outside the writable directories makes the tool ask the user to allow that directory for the session, or refuses if nobody can answer.

### `write`

Create or overwrite files.

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path (required) |
| `content` | string | File content (required) |

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

Database tools share a common interface. All support `query`, `list_tables`, `describe`, and `export_query` operations. By default, only SELECT queries are allowed. Mutation queries (INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE) need `readonly: false` on the connection in `databases.yaml`, and are always refused with `read-only` access.

| Parameter | Type | Description |
|---|---|---|
| `database` | string | Database alias (for multi-database configs) |
| `operation` | string | `query`, `list_tables`, `describe`, or `export_query` |
| `sql` | string | SQL query (for `query` and `export_query`) |
| `table_pattern` | string | Filter pattern for `list_tables` |
| `table_name` | string | Table name for `describe` |
| `output_path` | string | CSV output path for `export_query` |

Query results are formatted as ASCII tables with a default limit of 100 rows.

### Configuration

#### CLI users

The CLI reads database configuration from `~/.config/rho-agent/databases.yaml` (or the path in `RHO_AGENT_DB_CONFIG`). Any databases defined there are available to all CLI sessions automatically.

```yaml
# ~/.config/rho-agent/databases.yaml
databases:
  sales:
    type: sqlite
    path: /data/sales.db
  analytics:
    type: postgres
    host: localhost
    database: analytics
    user: reader
    password: ${ANALYTICS_PASSWORD}
```

#### Python SDK users

Set databases on `AgentConfig` directly — either in YAML agent config files or inline in code. No global config file is consulted; you get exactly the databases you configure.

**Agent config YAML:**

```yaml
# agents/data-analyst.yaml
system_prompt: You are a data analyst.
access: read-only
databases:
  sales:
    type: sqlite
    path: /data/sales.db
  analytics:
    type: postgres
    host: localhost
    database: analytics
    user: reader
    password: ${ANALYTICS_PASSWORD}
```

**Inline in Python:**

```python
config = AgentConfig(
    system_prompt="You are a data analyst.",
    access="read-only",
    databases={
        "sales": {"type": "sqlite", "path": "/data/sales.db"},
        "analytics": {
            "type": "postgres",
            "host": "localhost",
            "database": "analytics",
            "user": "reader",
            "password": "${ANALYTICS_PASSWORD}",
        },
    },
)
```

#### Environment variable interpolation

String values support `${ENV_VAR}` interpolation so secrets stay out of config files.

### Supported databases

| Type | Required fields | Optional fields | Install extra |
|---|---|---|---|
| `sqlite` | `path` | — | — |
| `postgres` | `database` | `host`, `port`, `user`, `password` | `uv pip install '.[db]'` |
| `mysql` | `database` | `host`, `port`, `user`, `password` | `uv pip install '.[db]'` |
| `oracle` | `dsn` | `user`, `password` | `uv pip install '.[db]'` |
| `vertica` | `database` | `host`, `port`, `user`, `password` | `uv pip install '.[db]'` |

## Daytona remote sandbox tools

When the Daytona backend is active (`--backend daytona`), all file and shell tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`) execute in a remote cloud sandbox. The tool names and parameter schemas are identical — the model sees the same interface. Database tools always run locally.

See the [Daytona](daytona/) guide for setup, configuration, and file upload/download.

## Sub-agent tools

rho-agent supports two patterns for spawning child agents. Both create independent sessions and disable further delegation to prevent unbounded recursion.

### `delegate` (ad-hoc delegation)

Spawn a child agent to execute a focused subtask. The LLM decides at runtime when to delegate. See [Architecture](architecture/) for details on multi-agent coordination.

| Parameter | Type | Description |
|---|---|---|
| `instruction` | string | Task description for the child agent (required) |
| `full_context` | boolean | Copy parent conversation history to child |

The child agent inherits the parent's access, access grants, model, and working directory. Delegation is single-level — child agents cannot delegate further. Parent cancellation propagates to children automatically.

### Agent-as-tool (pre-configured specialists)

Wrap a pre-configured agent as a named tool with typed parameters. Unlike `delegate`, the child has its own system prompt, access level, and model — configured at setup time, not derived from the parent.

```python
from rho_agent.tools.handlers import AgentToolHandler
from rho_agent.core.config import AgentConfig

sql_agent = AgentToolHandler(
    tool_name="generate_sql",
    tool_description="Generate SQL from a natural language question.",
    system_prompt="You are an expert SQL developer. ...",
    config=AgentConfig(model="gpt-6.1-terra", access="read-only"),
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
| `config` | AgentConfig | Child's model, access, etc. |
| `input_schema` | dict | JSON Schema for typed parameters |
| `input_formatter` | callable | Custom `(args) -> instruction` mapper |
| `requires_approval` | bool | Whether parent needs approval before calling |
