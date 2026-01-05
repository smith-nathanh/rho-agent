# ro-agent Architecture Map

## Overview

`ro-agent` is a **read-only research agent** for compute clusters. It assists developers by inspecting logs, probing databases, and finding documentation—without the ability to modify existing files.

```
┌─────────────────────────────────────────────────────────────────────┐
│                              CLI (cli.py)                          │
│  - Entry point, REPL, argument parsing, approval prompts           │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Agent (core/agent.py)                      │
│  - Orchestrates conversation loop                                   │
│  - Manages tool execution and streaming                            │
│  - Handles auto-compaction                                          │
└─────────┬─────────────────────┬─────────────────────┬───────────────┘
          │                     │                     │
          ▼                     ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│     Session     │  │   ModelClient    │  │     ToolRegistry         │
│ (core/session)  │  │ (client/model)   │  │   (tools/registry)       │
│                 │  │                  │  │                          │
│ - History       │  │ - OpenAI API     │  │ - Stores handlers        │
│ - Token counts  │  │ - Streaming      │  │ - Dispatches invocations │
└─────────────────┘  └──────────────────┘  └───────────┬──────────────┘
                                                       │
                                                       ▼
                                     ┌─────────────────────────────────┐
                                     │       Tool Handlers             │
                                     │    (tools/handlers/*)           │
                                     └─────────────────────────────────┘
```

---

## Execution Flow

### User Input → Model → Tools → Response

```
User types message
        │
        ▼
Session.add_user_message()
        │
        ▼
Auto-compact check (80% of 100k tokens?)
        │
        ▼
Build Prompt (system + history + tool_specs)
        │
        ▼
ModelClient.stream() → OpenAI-compatible API
        │
        ├─── text event ────────► yield to CLI for display
        │
        └─── tool_call event ───► ToolRegistry.dispatch()
                                        │
                                        ▼
                                  Handler.handle()
                                        │
                                        ▼
                                  ToolOutput (content, success, metadata)
                                        │
                                        ▼
                                  Add result to Session.history
                                        │
                                        ▼
                        Loop back to model if tools were called
                                        │
                                        ▼
                              turn_complete (no more tool calls)
```

---

## Tool Handler Architecture

All handlers inherit from `ToolHandler` (`tools/base.py:26`):

```python
class ToolHandler(ABC):
    @property
    def name(self) -> str: ...           # Unique identifier
    @property
    def description(self) -> str: ...    # LLM-friendly description
    @property
    def parameters(self) -> dict: ...    # JSON Schema for args
    @property
    def requires_approval(self) -> bool: # Default: False
        return False

    async def handle(self, invocation: ToolInvocation) -> ToolOutput: ...
```

**Data Flow:**
```
ToolInvocation(call_id, tool_name, arguments)
        │
        ▼
    handler.handle()
        │
        ▼
ToolOutput(content: str, success: bool, metadata: dict)
```

---

## Core File Inspection Tools

### `grep_files` — Pattern Search (`tools/handlers/grep_files.py`)

**Purpose:** Search file contents with regex patterns. Mirrors Claude Code's Grep tool.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `pattern` | string | ✓ | Regex pattern |
| `path` | string | ✓ | Directory or file to search |
| `glob` | string | | Filter files (e.g., `*.py`) |
| `output_mode` | enum | | `files_with_matches` (default), `content`, `count` |
| `ignore_case` | bool | | Case-insensitive search |
| `context_lines` | int | | Lines before/after match |
| `head_limit` | int | | Max results (default: 50) |

**Key Implementation Details:**

1. **File Collection** (`_collect_files` line 139):
   - Walks directory tree
   - Filters by glob pattern
   - Skips `.git`, `__pycache__`, `node_modules`, `.venv`
   - Max 500 files per search

2. **Three Output Modes:**
   - `files_with_matches`: Just file paths that contain the pattern
   - `content`: Matching lines with optional context (prefixed `>` for matches)
   - `count`: Match counts per file in format `   123  /path/to/file.py`

3. **Safety:** Lines truncated at 500 chars

**Requires Approval:** No

---

### `list_dir` — Directory Exploration (`tools/handlers/list_dir.py`)

**Purpose:** List directory contents with metadata. Two modes: flat (ls-like) or recursive (tree view).

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | ✓ | Directory to list |
| `show_hidden` | bool | | Include dotfiles |
| `recursive` | bool | | Tree view mode |
| `max_depth` | int | | Depth limit for recursive (default: 3) |

**Key Implementation Details:**

1. **Flat Mode** (`_list_flat` line 88):
   ```
   -rw-r--r--    1.2KB  2024-01-05 10:30  file.txt
   drwxr-xr-x        -  2024-01-05 09:00  subdir/
   ```
   - Permissions, size, mtime, name
   - Directories sorted first, then files
   - Shows symlink targets

2. **Recursive Mode** (`_list_recursive` line 134):
   ```
   ├── src/
   │   ├── main.py (2.3KB)
   │   └── utils.py (1.1KB)
   └── README.md (512B)
   ```
   - Tree connectors (`├──`, `└──`, `│`)
   - Depth-limited traversal

3. **Limits:** Max 200 entries per listing

**Requires Approval:** No

---

### `read_file` — File Reading

**Purpose:** Read text files with optional line range support.

**Key Features:**
- Line range support (`start_line`, `end_line`)
- Binary file detection blocks images, PDFs, compiled files
- Max 500 lines default
- Output includes line numbers

**Requires Approval:** No

---

### `shell` — Command Execution (`tools/handlers/shell.py`)

**Purpose:** Execute shell commands for text-based inspection. **Read-only by design.**

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string | ✓ | Shell command to execute |
| `working_dir` | string | | Override working directory |

**Safety Architecture:**

1. **Allowlist** (lines 12-100): ~50 safe read-only commands
   ```python
   ALLOWED_COMMANDS = {
       # File inspection
       "cat", "head", "tail", "less", "more",
       # Search
       "grep", "rg", "ag", "find", "locate",
       # Text processing
       "awk", "sed", "cut", "sort", "uniq", "jq", "yq",
       # Git (read-only)
       "git",
       # System info
       "ps", "top", "df", "du",
       ...
   }
   ```

2. **Dangerous Pattern Blocking** (lines 103-146):
   ```python
   DANGEROUS_PATTERNS = [
       ">", ">>",           # Redirects
       "rm ", "mv ", "cp ", # File ops
       "chmod", "chown",    # Permissions
       "sudo", "su ",       # Privilege escalation
       "pip ", "npm ",      # Package managers
       ...
   ]
   ```

3. **Command Extraction** (`extract_base_command` line 149):
   - Handles pipes: `cat foo | grep bar` → checks `cat`
   - Handles chaining: `cmd1 && cmd2` → checks `cmd1`
   - Strips env vars: `VAR=x cmd` → checks `cmd`

**Execution Flow:**
```
is_command_allowed(command)
        │
        ├── Check DANGEROUS_PATTERNS → block if found
        │
        └── Extract base command → check ALLOWED_COMMANDS
                │
                ▼
asyncio.create_subprocess_shell()
        │
        ▼
Timeout after 120 seconds
        │
        ▼
Return stdout + stderr (labeled)
```

**Requires Approval:** Yes

---

### `write_output` — Export Findings

**Purpose:** Create new output files (reports, summaries, scripts).

**Safety:**
- Blocks overwriting existing files
- Blocks sensitive paths (`.bashrc`, `.ssh/`, `/etc/`)
- Creates parent directories automatically

**Requires Approval:** Yes

---

## ToolRegistry — Dispatch System (`tools/registry.py`)

Simple registry pattern for tool management:

```python
class ToolRegistry:
    _handlers: dict[str, ToolHandler]

    def register(handler):        # Store handler by name
    def get(name) -> Handler:     # Lookup by name
    def get_specs() -> list:      # OpenAI function calling format
    def requires_approval(name):  # Check approval flag
    def dispatch(invocation):     # Route to handler
```

**Registration** happens in `cli.py` (`create_registry` function):
```python
registry = ToolRegistry()
registry.register(ReadFileHandler())
registry.register(ListDirHandler())
registry.register(GrepFilesHandler())
registry.register(ShellHandler(working_dir))
registry.register(WriteOutputHandler())
# + database handlers if env vars present
```

---

## Database Handlers (High-Level)

All database handlers inherit from `DatabaseHandler` (`tools/handlers/database.py:81`):

```
┌────────────────────────────────────────────────────────────┐
│                    DatabaseHandler (ABC)                   │
│                                                            │
│  Operations:                                               │
│    - query: Execute read-only SQL                          │
│    - list_tables: Find tables by pattern                   │
│    - describe: Get table schema details                    │
│                                                            │
│  Safety: MUTATION_PATTERNS blocks INSERT/UPDATE/DELETE/etc │
└──────────────────────┬─────────────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   Oracle    │ │   SQLite    │ │   Vertica   │
│   Handler   │ │   Handler   │ │   Handler   │
└─────────────┘ └─────────────┘ └─────────────┘
```

**Shared Features:**
- SQL mutation detection via regex (blocks `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, etc.)
- ASCII table formatting for results
- Row limit support (default: 100)
- All require approval

**Abstract Methods Each Implements:**
- `_get_connection()` — Database-specific connection
- `_execute_query()` — Run SQL, return (columns, rows)
- `_get_list_tables_sql()` — Catalog query for table listing
- `_get_describe_sql()` — Catalog query for column info

**Environment Variables:**
- Oracle: `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`
- SQLite: `SQLITE_DB`
- Vertica: `VERTICA_HOST`, `VERTICA_PORT`, `VERTICA_DATABASE`, `VERTICA_USER`, `VERTICA_PASSWORD`

---

## Session & Context Management (`core/session.py`)

Stores conversation state in OpenAI message format:

```python
Session:
    system_prompt: str
    history: list[dict]  # OpenAI message format
    total_input_tokens: int
    total_output_tokens: int
```

**Methods:**
- `add_user_message()` / `add_assistant_message()`
- `add_assistant_tool_calls()` / `add_tool_results()`
- `replace_with_summary()` — For compaction
- `estimate_tokens()` — Rough count for auto-compact

---

## Agent Loop (`core/agent.py`)

The `Agent` class orchestrates everything:

```python
Agent(session, registry, client, approval_callback)
    │
    └── run_turn(user_input) → AsyncIterator[AgentEvent]
            │
            ├── Check auto-compact (80% threshold)
            │
            ├── Build Prompt with history + tools
            │
            ├── Stream response from model
            │
            ├── For each tool_call:
            │       - Check approval if required
            │       - Dispatch to registry
            │       - Truncate output (max 20k chars)
            │       - Record in history
            │
            └── Loop until no more tool calls
```

**Event Types:**
- `text` — Streamed text content
- `tool_start` — Tool invocation beginning
- `tool_end` — Tool result with metadata
- `tool_blocked` — User rejected command
- `turn_complete` — Turn finished with usage stats
- `compact_start`/`compact_end` — Compaction events

---

## Key Constants & Limits

| Limit | Value | Location |
|-------|-------|----------|
| Tool output truncation | 20,000 chars | `agent.py:13` |
| Context limit | 100,000 tokens | `agent.py:16` |
| Auto-compact threshold | 80% | `agent.py:17` |
| Shell timeout | 120 seconds | `shell.py:9` |
| Max grep results | 50 | `grep_files.py:11` |
| Max files per grep | 500 | `grep_files.py:12` |
| Max line length | 500 chars | `grep_files.py:13` |
| Max dir entries | 200 | `list_dir.py:12` |
| Max DB rows | 100 | `database.py:29` |

---

This architecture follows the **Codex CLI pattern** with a clean separation between the agent loop, tool dispatch, and individual handlers. The read-only constraint is enforced at multiple levels: allowlisted commands in shell, SQL mutation detection in databases, and the explicit absence of file write tools (except `write_output` for exports).
