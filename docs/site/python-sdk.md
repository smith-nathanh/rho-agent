---
title: Python SDK
description: Create and run agents programmatically.
order: 5
---

The API lets you create and run agents from code. The core pattern is: configure an `AgentConfig`, create an `Agent`, open a `Session`, and call `run()`.

## Basic usage

```python
import asyncio
from rho_agent import Agent, AgentConfig, Session, SessionStore

config = AgentConfig(
    system_prompt="You are a research assistant.",
    profile="developer",
    model="gpt-5-mini",
    working_dir="/tmp/work",
)

agent = Agent(config)
session = Session(agent)

async def main() -> None:
    result = await session.run(prompt="Analyze recent failures and summarize root causes.")
    print(result.text)

asyncio.run(main())
```

## AgentConfig

A dataclass holding all configuration for an agent. Can be constructed directly or loaded from YAML.

```python
@dataclass
class AgentConfig:
    system_prompt: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    model: str = "gpt-5-mini"
    profile: str | PermissionProfile | None = None
    backend: str | DaytonaBackend = "local"  # "local", "daytona", or DaytonaBackend
    working_dir: str | None = None
    base_url: str | None = None
    service_tier: str | None = None
    reasoning_effort: str | None = None
    response_format: dict | None = None
    auto_approve: bool = True
    extras: dict[str, Any] = field(default_factory=dict)
```

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `system_prompt` | `str \| None` | `None` | System prompt text or path to a prompt template file |
| `vars` | `dict[str, str]` | `{}` | Variables for prompt template substitution |
| `model` | `str` | `"gpt-5-mini"` | Model identifier passed to the LLM client |
| `profile` | `str \| PermissionProfile \| None` | `None` | Permission profile name, object, or path to YAML |
| `backend` | `str \| DaytonaBackend` | `"local"` | Execution backend: `"local"`, `"daytona"`, or a `DaytonaBackend` instance |
| `working_dir` | `str \| None` | `None` | Working directory for tool execution |
| `base_url` | `str \| None` | `None` | Custom API base URL |
| `service_tier` | `str \| None` | `None` | API service tier |
| `reasoning_effort` | `str \| None` | `None` | Reasoning effort level (model-specific) |
| `response_format` | `dict \| None` | `None` | Structured output format specification |
| `auto_approve` | `bool` | `True` | Auto-approve tool calls without prompting |
| `extras` | `dict[str, Any]` | `{}` | Arbitrary metadata passed through to session |

### Loading and saving

```python
# Load from a YAML file
config = AgentConfig.from_file("my-agent.yaml")

# Save to a YAML file
config.to_file("my-agent.yaml")

# Resolve system prompt (handles template files and variable substitution)
resolved = config.resolve_system_prompt()
```

## Agent

A stateless, reusable agent definition. Holds the resolved config and tool registry. You can create multiple sessions from the same agent.

```python
agent = Agent(config)

# Properties
agent.config            # AgentConfig
agent.registry          # ToolRegistry (built from profile)
agent.system_prompt     # Resolved system prompt string

# Create an LLM client instance
client = agent.create_client()
```

### Customizing tools before creating a session

```python
agent = Agent(config)

# Add a custom tool handler before opening a session
agent.registry.register(my_custom_handler)

session = Session(agent)
```

## State

Pure conversation data container. Tracks messages, token usage, status, and provides serialization. State is created internally by Session but can be inspected and observed.

```python
state = session.state

# Messages
state.messages                        # Full message list
state.get_messages()                  # Messages for model context
state.add_user_message(content)
state.add_assistant_message(content)
state.add_tool_result(tool_call_id, result)

# Usage
state.usage                           # dict with token counts
state.update_usage(input=100, output=50)

# Status
state.status                          # "running", "completed", "error", "cancelled"
state.run_count                       # Number of runs completed

# Token estimation
tokens = state.estimate_tokens()

# Context compaction
state.replace_with_summary(summary, tokens_before, tokens_after)

# Observers
state.add_observer(my_observer)
state.remove_observer(my_observer)

# Serialization
state.to_jsonl(path)                  # Append-only write to trace file
restored = State.from_jsonl(path)     # Replay trace to restore state
```

## Session

The execution context for running prompts. Created from an Agent, it owns the State and drives the agent loop.

### Constructor

```python
session = Session(agent)
```

### Settable attributes

```python
session.approval_callback = my_callback    # Called when tool needs approval
session.cancel_check = lambda: False       # Polled to check for cancellation
session.auto_compact = True                # Auto-compact when context is large
session.context_window = 128_000           # Context window size for compaction
```

### Methods

```python
# Run a prompt to completion
result = await session.run(prompt="Analyze the logs.")

# Cancel a running session
await session.cancel()

# Manually trigger context compaction
compact_result = await session.compact()

# Access state
session.state  # State object

# Get the Daytona sandbox (see Daytona guide for details)
sandbox = await session.get_sandbox()
```

### Async context manager (Daytona backend)

When using the Daytona backend, use `Session` as an async context manager to ensure the sandbox is cleaned up:

```python
async with Session(Agent(AgentConfig(backend="daytona", profile="developer"))) as session:
    result = await session.run(prompt="Check the deployment status.")
    print(result.text)
# sandbox deleted automatically
```

For the local backend, `close()` is a no-op and the context manager is optional. See the [Daytona](daytona/) guide for more.

## SessionStore

Manages session directories at `~/.config/rho-agent/sessions/`. Handles creation, listing, and resumption of sessions.

```python
store = SessionStore()                          # Uses default path
store = SessionStore("/custom/sessions/dir")

# Create a new session with persistence
session = store.create_session(agent)

# Resume a previous session
session = store.resume(session_id, agent)

# List all sessions
infos: list[SessionInfo] = store.list()

# Get the most recent session ID
latest_id = store.get_latest_id()
```

### SessionInfo

```python
@dataclass
class SessionInfo:
    id: str
    status: str
    created_at: datetime
    model: str
    profile: str
    first_prompt: str
```

## RunResult

Returned by `session.run()`. Contains the agent's response and metadata for the run.

```python
@dataclass
class RunResult:
    text: str                      # Final text response
    events: list[AgentEvent]       # All events from the run
    status: str                    # "completed", "error", "cancelled"
    usage: dict[str, int]          # Token counts for this run
```

## AgentEvent

Events emitted during a run. Each event has a `type` field and additional fields depending on the type.

```python
@dataclass
class AgentEvent:
    type: str
    content: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict | None = None
    tool_result: str | None = None
    tool_metadata: dict | None = None
    usage: dict | None = None
```

### Event types

| Type | Populated fields | Description |
|---|---|---|
| `text` | `content` | Streamed text chunk from the model |
| `tool_start` | `tool_name`, `tool_call_id`, `tool_args` | Tool execution begins |
| `tool_end` | `tool_name`, `tool_call_id`, `tool_result`, `tool_metadata` | Tool execution completes |
| `tool_blocked` | `tool_name`, `tool_call_id`, `tool_args` | Tool call denied by approval or permissions |
| `api_call_complete` | `usage` | LLM API call finished with token counts |
| `turn_complete` | `content`, `usage` | Agent turn completed (final response) |
| `compact_start` | — | Context compaction begins |
| `compact_end` | `content` | Context compaction completes with summary |
| `error` | `content` | Error occurred during execution |
| `cancelled` | `content` | Run was cancelled |
| `interruption` | `content` | Approval required (raises `ApprovalInterrupt`) |

### CompactResult

Returned by `session.compact()`:

```python
@dataclass
class CompactResult:
    summary: str
    tokens_before: int
    tokens_after: int
    trigger: str                   # "auto" or "manual"
```

## Patterns

### Multi-run conversation

Run multiple prompts in a single session, building on prior context:

```python
agent = Agent(AgentConfig(system_prompt="You are a database analyst.", profile="readonly"))
session = Session(agent)

r1 = await session.run(prompt="List all tables in the database.")
print(r1.text)

r2 = await session.run(prompt="Show me the schema for the users table.")
print(r2.text)

r3 = await session.run(prompt="Find users who signed up in the last 7 days.")
print(r3.text)
```

### Parallel dispatch

Run multiple agents concurrently using `asyncio.create_task` and `gather`:

```python
import asyncio
from rho_agent import Agent, AgentConfig, Session

async def analyze(task_prompt: str, work_dir: str) -> str:
    config = AgentConfig(
        system_prompt="You are an analyst.",
        profile="readonly",
        working_dir=work_dir,
    )
    session = Session(Agent(config))
    result = await session.run(prompt=task_prompt)
    return result.text

async def main() -> None:
    tasks = [
        asyncio.create_task(analyze("Check error rates.", "/var/log/app1")),
        asyncio.create_task(analyze("Check latency metrics.", "/var/log/app2")),
        asyncio.create_task(analyze("Check disk usage.", "/var/log/app3")),
    ]
    results = await asyncio.gather(*tasks)
    for text in results:
        print(text)

asyncio.run(main())
```

### Custom tools

Register custom tool handlers before creating a session:

```python
from rho_agent import Agent, AgentConfig, Session

agent = Agent(AgentConfig(profile="developer"))
agent.registry.register(my_custom_handler)

session = Session(agent)
result = await session.run(prompt="Use my custom tool to process the data.")
```

### Save and resume via SessionStore

```python
from rho_agent import Agent, AgentConfig, SessionStore

store = SessionStore()
agent = Agent(AgentConfig(system_prompt="You are a research assistant.", profile="developer"))

# First session
session = store.create_session(agent)
await session.run(prompt="Start analyzing the codebase.")
session_id = session.state.session_id

# Later: resume
session = store.resume(session_id, agent)
await session.run(prompt="Continue where we left off.")
```

### Daytona backend

See the [Daytona](daytona/) guide for full CLI and Python API usage, including file uploads, sandbox configuration, and `DaytonaBackend`.
