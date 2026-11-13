---
title: Python SDK
description: Create and run agents programmatically.
order: 6
---

The API lets you create and run agents from code. The core pattern is: configure an `AgentConfig`, create an `Agent`, open a `Session`, and call `run()`.

## Install in your project

Use a project dependency install (not `uv tool install`) so `import rho_agent` works in your code:

```bash
uv add git+https://github.com/smith-nathanh/rho-agent
```

Optional extras:

```bash
uv add 'rho-agent[db] @ git+https://github.com/smith-nathanh/rho-agent'
uv add 'rho-agent[daytona] @ git+https://github.com/smith-nathanh/rho-agent'
```

## Basic usage

```python
import asyncio
from rho_agent import Agent, AgentConfig, Session, SessionStore

config = AgentConfig(
    system_prompt="You are a research assistant.",
    profile="developer",
    model="gpt-6.1-terra",
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
    model: str = "gpt-6.1-terra"
    profile: str | PermissionProfile | None = None
    backend: str | DaytonaBackend = "local"  # "local", "daytona", or DaytonaBackend
    working_dir: str | None = None
    base_url: str | None = None
    service_tier: str | None = None
    reasoning_effort: str | None = None
    response_format: dict | None = None
    auto_approve: bool = True
    auto_compact: bool = True
    compact_threshold: float | None = None   # fraction of the context window
    compact_at_tokens: int | None = None     # exact token count; wins over the fraction
    context_window: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
```

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `system_prompt` | `str \| None` | `None` | System prompt text or path to a prompt template file |
| `vars` | `dict[str, str]` | `{}` | Variables for prompt template substitution |
| `model` | `str` | `"gpt-6.1-terra"` | Model identifier passed to the LLM client |
| `profile` | `str \| PermissionProfile \| None` | `None` | Permission profile name, object, or path to YAML |
| `backend` | `str \| DaytonaBackend` | `"local"` | Execution backend: `"local"`, `"daytona"`, or a `DaytonaBackend` instance |
| `working_dir` | `str \| None` | `None` | Working directory for tool execution |
| `base_url` | `str \| None` | `None` | Custom API base URL. When set to a non-OpenAI host, Chat Completions is used instead of the Responses API (see [Model client](#model-client)) |
| `service_tier` | `str \| None` | `None` | API service tier |
| `reasoning_effort` | `str \| None` | `None` | Reasoning effort (`none`, `low`, `medium`, `high`, ...; model-specific). `None` uses the model's default |
| `response_format` | `dict \| None` | `None` | Structured output format specification |
| `auto_approve` | `bool` | `True` | Auto-approve tool calls without prompting |
| `auto_compact` | `bool` | `True` | Summarize older history automatically when the context grows large |
| `compact_threshold` | `float \| None` | `None` | Compact when input reaches this fraction of the context window. `None` uses `0.7`, kept below a model's higher price tier if it has one |
| `compact_at_tokens` | `int \| None` | `None` | Compact at this exact input token count. Wins over `compact_threshold` |
| `context_window` | `int \| None` | `None` | Context window size in tokens. `None` looks the model up in a built-in table |
| `project_instructions` | `bool` | `True` | Append `AGENTS.md` / `CLAUDE.md` from the working directory and its parents to the system prompt |
| `skills` | `bool` | `True` | List `SKILL.md` skills in the system prompt |
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

### Model client

`agent.create_client()` returns a `ModelClient`, which talks to OpenAI through the Responses API (`/v1/responses`). Reasoning and function tools work together there, including on models such as `gpt-6.1-sol` that do not accept tools on Chat Completions.

```python
from rho_agent.client.model import ModelClient

client = ModelClient(model="gpt-6.1-terra", reasoning_effort="low")
client.api            # "responses"

# A custom base URL on a non-OpenAI host uses Chat Completions instead
local = ModelClient(model="gpt-oss-120b", base_url="http://localhost:8000/v1")
local.api             # "chat"

# Override the choice, e.g. for a gateway in front of OpenAI
ModelClient(model="gpt-6.1-terra", base_url="https://gateway.example.com/v1", api="responses")
```

| Argument | Description |
|---|---|
| `reasoning_effort` | Sent as `reasoning.effort`. When set, `temperature` is not sent |
| `service_tier` | Passed through (`"flex"` also raises the request timeout to 15 minutes) |
| `response_format` | A Chat Completions style `response_format`; converted to the Responses `text.format` |
| `api` | `"responses"` or `"chat"`. Default: Responses, unless a base URL (argument or `OPENAI_BASE_URL`) points at a non-OpenAI host. The `RHO_AGENT_API` environment variable sets the same thing |
| `streaming` | `False` makes one non-streaming request per model call, for accounts or gateways that cannot stream a model |

Requests are stateless (`store=False`, no `previous_response_id`): the full history is sent each time, so compaction, retries and resuming from `trace.jsonl` need nothing kept on OpenAI's side. The session still works with chat-format messages and tool definitions; `ModelClient` converts them for each request.

### Customizing tools before creating a session

```python
agent = Agent(config)

# Add a custom tool handler before opening a session
agent.registry.register(my_custom_handler)

session = Session(agent)
```

## State

The conversation trajectory — messages, token usage, cost, and status. State accumulates across multiple `run()` calls and can be inspected after the fact or loaded from a trace file without a live Session.

```python
state = session.state

# Check cost and token usage after a run
state.usage["cost_usd"]              # Cumulative cost across all runs
state.usage["input_tokens"]          # Total input tokens
state.usage["output_tokens"]         # Total output tokens

# Inspect what happened
state.status                          # "running", "completed", "error", "cancelled"
state.run_count                       # Number of run() calls completed
state.messages                        # Full message list (OpenAI chat format; see below)
state.get_user_messages()             # Extract just the user prompts
```

Assistant messages from a reasoning model carry one extra key, `response_items`: the model's output items in order, with its reasoning in encrypted form. The client sends them back on later requests so the model keeps its reasoning across tool calls and after a resume. Treat the key as opaque and keep it when you copy messages around; dropping it is harmless but loses that reasoning.

### Loading a past session's state

Load a trace file to inspect a session offline — no live Session needed:

```python
from pathlib import Path
from rho_agent import State

state = State.from_jsonl(Path("~/.config/rho-agent/sessions/abc123/trace.jsonl").expanduser().read_bytes())
print(f"Runs: {state.run_count}, Cost: ${state.usage['cost_usd']:.4f}")

# What tools did the agent call?
for msg in state.messages:
    for tc in msg.get("tool_calls") or []:
        print(f"  {tc['function']['name']}")
```

### Observing events in real time

Attach a `StateObserver` to get notified on every state mutation (message added, tool called, etc.):

```python
class ToolAuditLog:
    def on_event(self, event: dict) -> None:
        if event.get("tool_calls"):
            for tc in event["tool_calls"]:
                print(f"[{event['ts']}] {tc['function']['name']}")

session.state.add_observer(ToolAuditLog())
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
# Or queue approvals for `rho-agent monitor` to answer (needs a SessionStore session):
#   from rho_agent.core.approvals import ApprovalQueue
#   session.approval_callback = ApprovalQueue(session)
session.cancel_check = lambda: False       # Polled to check for cancellation
# Compaction settings start from AgentConfig and can be changed per session
session.auto_compact = True                # Auto-compact when context is large
session.compact_threshold = 0.5            # Fraction of the context window
session.compact_at_tokens = 40_000         # Exact token count; wins over the fraction
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
    status: str                    # "completed", "error", "cancelled", "interrupted",
                                   # "max_turns", "budget_exceeded"
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
    trigger: str                   # "auto", "manual" or "overflow"
    error: str | None = None       # set when the summary call failed; history is unchanged
    method: str = "summary"        # "summary", or "prune" when trimming was enough
```

Automatic compaction works in two steps. It first trims long tool results in the
older part of the history, keeping the start and end of each. If that frees at
least 30% of the context, it stops there and makes no model call. Otherwise it
asks the model for a structured summary, and the history becomes: every message
from the user verbatim, the summary, then the most recent messages verbatim.
A manual `session.compact()` always writes a summary.

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

See the [Daytona](daytona/) guide for full CLI and Python API usage, including file uploads, setup commands, sandbox configuration, `env_vars`, `Image` objects, and `DaytonaBackend`.
