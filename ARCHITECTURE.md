# rho-agent Architecture

## Overview

`rho-agent` is a Python-based research agent with **configurable permission profiles**. The architecture separates agent definition (stateless) from execution (stateful), with all conversation data managed through session directories.

```
┌─────────────────────────────────────────────────────────────────────┐
│                              CLI (cli/)                              │
│  - Entry point, REPL, argument parsing, approval prompts            │
│  - --config flag loads AgentConfig from YAML                        │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Agent (core/agent.py)                            │
│  - Stateless definition: config + tools + model client              │
│  - Reusable across multiple sessions                                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Session (core/session.py)                         │
│  - Execution context: runs prompts, drives the agent loop           │
│  - Owns State, handles cancellation via sentinel files              │
│  - Events passed to on_event callback                               │
└─────────┬─────────────────────┬─────────────────────┬───────────────┘
          │                     │                     │
          ▼                     ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│      State      │  │   ModelClient    │  │     ToolRegistry         │
│  (core/state)   │  │ (client/model)   │  │   (tools/registry)       │
│                 │  │                  │  │                          │
│ - Messages      │  │ - OpenAI API     │  │ - Stores handlers        │
│ - Token counts  │  │ - Streaming      │  │ - Dispatches invocations │
│ - trace.jsonl   │  │ - StreamEvents   │  │ - Type coercion          │
└─────────────────┘  └──────────────────┘  └───────────┬──────────────┘
                                                       │
                                                       ▼
                                     ┌─────────────────────────────────┐
                                     │       Tool Handlers             │
                                     │    (tools/handlers/*)           │
                                     │                                 │
                                     │  bash, read, grep, glob, list,  │
                                     │  write, edit, sqlite, postgres, │
                                     │  mysql, oracle, vertica, ...    │
                                     └─────────────────────────────────┘
```

---

## Permission Profiles (`permissions/__init__.py`)

Profiles control what the agent can do. Three built-in profiles plus YAML custom profiles:

| Profile | Shell | File Write | Database | Approval | Use Case |
|---------|-------|------------|----------|----------|----------|
| `readonly` | RESTRICTED | OFF | READONLY | DANGEROUS | Safe research on production systems |
| `developer` | UNRESTRICTED | FULL | READONLY | GRANULAR | Local development with file editing |
| `eval` | UNRESTRICTED | FULL | MUTATIONS | NONE | Sandboxed benchmark execution |

### Permission Modes

**ShellMode:**
- `RESTRICTED`: Only allowlisted commands (grep, cat, find, etc.), dangerous patterns blocked
- `UNRESTRICTED`: Any command allowed (rely on container/sandbox)

**FileWriteMode:**
- `OFF`: No file writing
- `CREATE_ONLY`: Can create new files, cannot overwrite
- `FULL`: Full write/edit capabilities

**DatabaseMode:**
- `READONLY`: SELECT only, mutations blocked
- `MUTATIONS`: Full access including INSERT/UPDATE/DELETE

**ApprovalMode:**
- `ALL`: All tools require approval
- `DANGEROUS`: Only bash, write, edit, database tools require approval
- `GRANULAR`: Per-tool configuration via `approval_required_tools`
- `NONE`: No approval (for sandboxed environments)

### Tool Factory (`permissions/factory.py`)

Creates a `ToolRegistry` from a `PermissionProfile`:

```python
profile = PermissionProfile.developer()
factory = ToolFactory(profile)
registry = factory.create_registry(working_dir="/path/to/project")
```

The factory:
1. Registers core tools (read, grep, glob, list, read_excel)
2. Registers bash with restricted/unrestricted mode per profile
3. Registers write/edit tools if `file_write != OFF`
4. Registers database tools if environment variables are set

---

## Agent Loop (`core/session.py`)

The agent loop is driven by `Session.run()`. The `Agent` holds stateless configuration (config, model client, tool registry), while `Session` manages the execution context and conversation state.

```python
config = AgentConfig(
    system_prompt="You are a research assistant.",
    profile="developer",
    working_dir="/tmp/work",
)
agent = Agent(config)
session = Session(agent)

result = await session.run(prompt="Analyze recent failures.")
# result is a RunResult with text, status, usage
```

Events are passed to an `on_event` callback (not yielded):

```python
def handle_event(event: AgentEvent) -> None:
    if event.type == "text":
        print(event.content, end="")

session = Session(agent, on_event=handle_event)
result = await session.run(prompt="Analyze recent failures.")
```

### Execution Flow

```
session.run(prompt=...)
    │
    ▼
Auto-compact check (80% of 100k tokens?)
    │ yes
    ├──────► compact() → on_event(compact_start/compact_end)
    │
    ▼
State.add_user_message()
    │
    ▼
┌─────────────────────────────────────────┐
│              AGENT LOOP                 │
│                                         │
│  Build Prompt (system + history + tools)│
│              │                          │
│              ▼                          │
│  ModelClient.stream() ──► StreamEvents  │
│              │                          │
│         ┌────┴────┐                     │
│         │         │                     │
│    text event   tool_call event         │
│         │         │                     │
│         ▼         ▼                     │
│  on_event         Check approval        │
│  (type="text")    │                     │
│                   ├─ rejected ──► on_event(tool_blocked), break
│                   │                     │
│                   ▼                     │
│             ToolRegistry.dispatch()     │
│                   │                     │
│                   ▼                     │
│             on_event(tool_end)          │
│                   │                     │
│                   ▼                     │
│         Add results to State            │
│                   │                     │
│         Loop if tools were called ──────┤
│                                         │
└─────────────────────────────────────────┘
    │
    ▼
on_event(run_end) → return RunResult
```

### AgentEvent Types

Events passed to the `on_event` callback:

| Type | Fields | Description |
|------|--------|-------------|
| `run_start` | `content` | Run beginning |
| `run_end` | `content`, `usage` | Run finished, includes token counts |
| `message` | `content` | Streamed text from model |
| `llm_start` | `content` | Model call beginning |
| `llm_end` | `content`, `usage` | Model call finished |
| `api_call_complete` | `content`, `usage` | API round-trip complete |
| `tool_start` | `tool_name`, `tool_args` | Tool invocation beginning |
| `tool_end` | `tool_name`, `tool_result`, `tool_metadata` | Tool completed |
| `tool_blocked` | `tool_name`, `tool_args` | User rejected tool |
| `compact` | `content` (token summary) | Compaction performed |
| `usage` | `usage` | Token usage update |
| `interruption` | `content` | Turn was interrupted |
| `error` | `content` | Error occurred |

### Cancellation

The session supports mid-execution cancellation:

```python
session.cancel()  # Called from signal handler or another task
```

Cancellation works through a sentinel file written to the session directory. It is checked:
- Before each model call
- During streaming
- Before each tool execution

For external cancellation (e.g., from `rho-agent monitor`), writing a `cancel` sentinel file to the session directory triggers the same behavior.

---

## Tool System

### ToolHandler (`tools/base.py`)

Abstract base class for all tools:

```python
class ToolHandler(ABC):
    @property
    def name(self) -> str: ...           # "bash", "read", "grep", etc.

    @property
    def description(self) -> str: ...    # LLM-friendly description

    @property
    def parameters(self) -> dict: ...    # JSON Schema for arguments

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

### ToolRegistry (`tools/registry.py`)

Stores handlers and dispatches invocations:

```python
registry = ToolRegistry()
registry.register(BashHandler(restricted=True))
registry.register(ReadHandler())

# Get specs for LLM
specs = registry.get_specs()  # OpenAI function calling format

# Dispatch invocation
output = await registry.dispatch(invocation)
```

The registry handles:
- Type coercion (LLMs sometimes pass strings for booleans/integers)
- Error handling (returns error as `ToolOutput`, doesn't crash)
- Cancellation propagation

### Available Tool Handlers

| Handler | Name | Description |
|---------|------|-------------|
| `BashHandler` | `bash` | Shell execution (restricted or unrestricted) |
| `ReadHandler` | `read` | Read file contents with line ranges |
| `GrepHandler` | `grep` | Search file contents with ripgrep |
| `GlobHandler` | `glob` | Find files by pattern |
| `ListHandler` | `list` | List directory contents |
| `WriteHandler` | `write` | Create/overwrite files |
| `EditHandler` | `edit` | Edit files with search/replace |
| `ReadExcelHandler` | `read_excel` | Read Excel/CSV files |
| `SqliteHandler` | `sqlite` | SQLite queries |
| `PostgresHandler` | `postgres` | PostgreSQL queries |
| `MysqlHandler` | `mysql` | MySQL queries |
| `OracleHandler` | `oracle` | Oracle queries |
| `VerticaHandler` | `vertica` | Vertica queries |

---

## Session Management

### State (`core/state.py`)

Stores conversation data and writes trace events:

```python
@dataclass
class State:
    system_prompt: str
    messages: list[dict]          # OpenAI message format
    total_input_tokens: int
    total_output_tokens: int
```

**Key Methods:**
- `add_user_message()` / `add_assistant_message()`
- `add_assistant_tool_calls()` / `add_tool_results()`
- `replace_with_summary()` — For compaction
- `estimate_tokens()` — Rough count (4 chars ~ 1 token)
- `write_event()` — Appends structured event to `trace.jsonl`

### SessionStore (`core/session_store.py`)

Manages session directories under `~/.config/rho-agent/sessions/`:

```
~/.config/rho-agent/sessions/
  abc123/
    config.yaml       # AgentConfig snapshot
    trace.jsonl        # Event log (run_start, llm_start, tool_end, ...)
    meta.json          # Session metadata (status, timestamps, labels)
    cancel             # Sentinel: triggers cancellation
    pause              # Sentinel: pauses execution
    directives.jsonl   # Sentinel: external instructions appended here
```

**trace.jsonl events:**

| Event | Description |
|-------|-------------|
| `run_start` | New run beginning |
| `run_end` | Run finished with status |
| `message` | Assistant text output |
| `llm_start` | Model API call initiated |
| `llm_end` | Model API call completed |
| `tool_start` | Tool invocation beginning |
| `tool_end` | Tool execution completed |
| `tool_blocked` | Tool rejected by approval |
| `compact` | Context compaction performed |
| `usage` | Token usage snapshot |

### Context Compaction

When context exceeds 80% of limit, the session auto-compacts:

1. Formats history as text
2. Asks model to summarize progress and next steps
3. Replaces history with summary + recent user messages
4. Preserves last 2-3 user messages for continuity

```python
result = await session.compact(custom_instructions="Focus on the database schema")
# CompactResult(summary, tokens_before, tokens_after, trigger)
```

---

## Model Client (`client/model.py`)

Streaming client for OpenAI-compatible APIs:

```python
client = ModelClient(
    model="gpt-5-mini",
    base_url="https://api.openai.com/v1",  # or vLLM, etc.
    service_tier="flex",  # Optional: 50% cost savings
)
```

### StreamEvent Types

Events from `client.stream()`:

| Type | Fields | Description |
|------|--------|-------------|
| `text` | `content` | Text delta |
| `tool_call` | `tool_call` (ToolCall) | Complete tool call |
| `done` | `usage` | Stream finished |
| `error` | `content` | Error message |

The client handles:
- Streaming with `stream_options={"include_usage": True}`
- Tool call assembly from deltas
- Non-streaming fallback for providers that don't support streaming tools (e.g., Cerebras)

---

## Multi-Agent Coordination

### Delegation

Agents can delegate subtasks to child agents. A parent agent spawns a child `Session` with a scoped config, runs a prompt, and incorporates the result:

```python
child_config = AgentConfig(system_prompt="You are a SQL expert.", profile="readonly")
child_agent = Agent(child_config)
child_session = Session(child_agent)
result = await child_session.run(prompt="Find slow queries in the orders table")
await child_session.close()
```

### Monitor

The `rho-agent monitor <dir>` command operates on session directories to observe and control running agents:

| Subcommand | Description |
|------------|-------------|
| `ps` | List active sessions in the directory |
| `watch` | Tail `trace.jsonl` for a session (live event stream) |
| `cancel` | Write `cancel` sentinel to stop a session |
| `pause` | Write `pause` sentinel to pause a session |
| `resume` | Remove `pause` sentinel to resume |
| `directive` | Append an instruction to `directives.jsonl` |

All coordination happens through the filesystem — no database or network transport required.

---

## Key Constants

| Constant | Value | Location |
|----------|-------|----------|
| Tool output truncation | 20,000 chars (head+tail) | `session.py` |
| Context limit | 100,000 tokens | `session.py` |
| Auto-compact threshold | 80% | `session.py` |
| Bash timeout (restricted) | 120 seconds | `bash.py` |
| Bash timeout (unrestricted) | 300 seconds | `bash.py` |

---

## Key Directories

| Directory | Contents |
|-----------|----------|
| `rho_agent/core/` | `agent.py`, `session.py`, `state.py`, `config.py`, `events.py`, `session_store.py` |
| `rho_agent/permissions/` | Permission profiles, modes, factory (was `capabilities/`) |
| `rho_agent/tools/` | `base.py`, `registry.py`, `handlers/` |
| `rho_agent/client/` | `model.py` (OpenAI-compatible streaming client) |
| `rho_agent/cli/` | CLI entry point, REPL, monitor subcommands |
| `rho_agent/eval/` | Benchmark integrations (BIRD-Bench, Harbor/TerminalBench) |

---

## Public Exports

The top-level `rho_agent` package exports:

```python
from rho_agent import Agent, AgentConfig, AgentEvent, RunResult, Session, SessionStore, State
```

---

## Architecture Principles

1. **Configurable permissions**: Profiles control what tools are available and how they behave
2. **Event-driven streaming**: Session passes events to callbacks for real-time UI updates
3. **Approval workflow**: Dangerous operations require explicit user approval
4. **Graceful degradation**: Tool errors return results to agent for self-correction
5. **Cancellation support**: Mid-execution cancellation via sentinel files
6. **Context management**: Auto-compaction prevents context overflow
