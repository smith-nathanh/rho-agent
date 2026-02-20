# rho-agent Architecture

## Overview

`rho-agent` is a Python-based research agent with **configurable capability profiles**. The core agent harness orchestrates a conversation loop between an LLM and a set of tools, with streaming responses and approval workflows.

```
┌─────────────────────────────────────────────────────────────────────┐
│                              CLI (cli/)                              │
│  - Entry point, REPL, argument parsing, approval prompts            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Agent (core/agent.py)                       │
│  - Orchestrates conversation loop                                   │
│  - Streams AgentEvents to caller                                    │
│  - Handles tool execution and auto-compaction                       │
└─────────┬─────────────────────┬─────────────────────┬───────────────┘
          │                     │                     │
          ▼                     ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│     Session     │  │   ModelClient    │  │     ToolRegistry         │
│ (core/session)  │  │ (client/model)   │  │   (tools/registry)       │
│                 │  │                  │  │                          │
│ - History       │  │ - OpenAI API     │  │ - Stores handlers        │
│ - Token counts  │  │ - Streaming      │  │ - Dispatches invocations │
│ - Compaction    │  │ - StreamEvents   │  │ - Type coercion          │
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

## Capability Profiles (`capabilities/__init__.py`)

Profiles control what the agent can do. Three built-in profiles plus YAML custom profiles:

| Profile | Shell | File Write | Database | Approval | Use Case |
|---------|-------|------------|----------|----------|----------|
| `readonly` | RESTRICTED | OFF | READONLY | DANGEROUS | Safe research on production systems |
| `developer` | UNRESTRICTED | FULL | READONLY | GRANULAR | Local development with file editing |
| `eval` | UNRESTRICTED | FULL | MUTATIONS | NONE | Sandboxed benchmark execution |

### Capability Modes

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

### Tool Factory (`capabilities/factory.py`)

Creates a `ToolRegistry` from a `CapabilityProfile`:

```python
profile = CapabilityProfile.developer()
factory = ToolFactory(profile)
registry = factory.create_registry(working_dir="/path/to/project")
```

The factory:
1. Registers core tools (read, grep, glob, list, read_excel)
2. Registers bash with restricted/unrestricted mode per profile
3. Registers write/edit tools if `file_write != OFF`
4. Registers database tools if environment variables are set

---

## Agent Loop (`core/agent.py`)

The `Agent` class orchestrates the conversation:

```python
agent = Agent(
    session=session,
    registry=registry,
    client=client,
    approval_callback=approval_handler.check_approval,
)

async for event in agent.run_turn(user_input):
    handle_event(event)
```

### Execution Flow

```
User Input
    │
    ▼
Auto-compact check (80% of 100k tokens?)
    │ yes
    ├──────► compact() → yield compact_start/compact_end
    │
    ▼
Session.add_user_message()
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
│  yield AgentEvent  Check approval       │
│  (type="text")     │                    │
│                    ├─ rejected ──► yield tool_blocked, break
│                    │                    │
│                    ▼                    │
│              ToolRegistry.dispatch()    │
│                    │                    │
│                    ▼                    │
│              yield tool_end             │
│                    │                    │
│                    ▼                    │
│         Add results to Session          │
│                    │                    │
│         Loop if tools were called ──────┤
│                                         │
└─────────────────────────────────────────┘
    │
    ▼
yield turn_complete (no more tool calls)
```

### AgentEvent Types

Events yielded by `agent.run_turn()`:

| Type | Fields | Description |
|------|--------|-------------|
| `text` | `content` | Streamed text from model |
| `tool_start` | `tool_name`, `tool_args` | Tool invocation beginning |
| `tool_end` | `tool_name`, `tool_result`, `tool_metadata` | Tool completed |
| `tool_blocked` | `tool_name`, `tool_args` | User rejected tool |
| `compact_start` | `content` ("auto" or "manual") | Compaction beginning |
| `compact_end` | `content` (token summary) | Compaction finished |
| `turn_complete` | `usage` | Turn finished, includes token counts |
| `cancelled` | `content` | Turn was cancelled |
| `error` | `content` | Error occurred |

### Cancellation

The agent supports mid-turn cancellation:

```python
agent.request_cancel()  # Called from signal handler
```

Cancellation is checked:
- Before each model call
- During streaming
- Before each tool execution

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

## Session Management (`core/session.py`)

Stores conversation state in OpenAI message format:

```python
@dataclass
class Session:
    system_prompt: str
    history: list[dict]        # OpenAI message format
    total_input_tokens: int
    total_output_tokens: int
```

**Key Methods:**
- `add_user_message()` / `add_assistant_message()`
- `add_assistant_tool_calls()` / `add_tool_results()`
- `replace_with_summary()` — For compaction
- `estimate_tokens()` — Rough count (4 chars ≈ 1 token)

### Context Compaction

When context exceeds 80% of limit, the agent auto-compacts:

1. Formats history as text
2. Asks model to summarize progress and next steps
3. Replaces history with summary + recent user messages
4. Preserves last 2-3 user messages for continuity

```python
result = await agent.compact(custom_instructions="Focus on the database schema")
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

## Key Constants

| Constant | Value | Location |
|----------|-------|----------|
| Tool output truncation | 20,000 chars (head+tail) | `agent.py:14` |
| Context limit | 100,000 tokens | `agent.py:17` |
| Auto-compact threshold | 80% | `agent.py:18` |
| Bash timeout (restricted) | 120 seconds | `bash.py:14` |
| Bash timeout (unrestricted) | 300 seconds | `bash.py:15` |

---

## Architecture Principles

1. **Configurable capabilities**: Profiles control what tools are available and how they behave
2. **Event-driven streaming**: Agent yields events for real-time UI updates
3. **Approval workflow**: Dangerous operations require explicit user approval
4. **Graceful degradation**: Tool errors return results to agent for self-correction
5. **Cancellation support**: Mid-turn cancellation at multiple checkpoints
6. **Context management**: Auto-compaction prevents context overflow
