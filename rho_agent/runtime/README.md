# Runtime Package

The runtime package owns the full lifecycle of an agent session: construction,
tool registration, execution, interrupt/resume, reconfiguration, background
dispatch, and teardown.

## Module Map

| Module                | Purpose |
|-----------------------|---------|
| `protocol.py`         | `Runtime` — the structural (duck-typed) protocol every backend must satisfy |
| `types.py`            | Concrete types: `LocalRuntime`, `RunState`, `RunResult`, `ToolApprovalItem`, `SessionUsage` |
| `options.py`          | `RuntimeOptions` — dataclass of all knobs (model, profile, working dir, observability, etc.) |
| `factory.py`          | `create_runtime()` — top-level constructor; returns `LocalRuntime` or `DaytonaRuntime` |
| `builder.py`          | `build_runtime_registry()` — shared registry construction used by both create and reconfigure |
| `reconfigure.py`      | `reconfigure_runtime()` — hot-swap profile/tools on a live `LocalRuntime` (powers `/mode`) |
| `registry_extensions.py` | Registers tools that need runtime context (currently just `DelegateHandler`) |
| `run.py`              | `run_prompt()` / `run_prompt_stored()` — execute one turn, with optional state persistence |
| `store.py`            | `RunStore` protocol + `SqliteRunStore` — persistence backend for interrupted run state |
| `dispatch.py`         | `dispatch_prompt()` / `AgentHandle` — fire-and-forget background execution for orchestrators |
| `cancellation.py`     | `CancellationToken` — cooperative cancellation primitive |
| `daytona.py`          | `DaytonaRuntime` — cloud sandbox backend; tools execute in a remote VM |

## Runtime Protocol

`Runtime` (in `protocol.py`) is a `@runtime_checkable` Protocol. Any backend
must expose:

- **Collaborators**: `agent`, `session`, `registry`, `options`, `observability`
- **Identity**: `model`, `profile_name`, `session_id`
- **Approval/cancel**: `approval_callback`, `cancel_check`
- **Lifecycle**: `start()`, `close(status)`, plus `async with` support
- **State**: `capture_state(interruptions) -> RunState`, `restore_state(RunState)`

Two concrete implementations exist: `LocalRuntime` (tools run in-process) and
`DaytonaRuntime` (tools run in a remote sandbox).

## Construction

`create_runtime()` is the main entry point:

1. Resolves `RuntimeOptions` (model, profile, working dir, etc.)
2. Calls `build_runtime_registry()` to create the tool registry from the capability profile
3. Creates `ModelClient` and `Agent`
4. Builds observability processor if configured
5. If profile is `"daytona"`, registers Daytona remote handlers and returns a `DaytonaRuntime`; otherwise returns a `LocalRuntime`

The shared `build_runtime_registry()` path is also used by `reconfigure_runtime()`,
so both create and reconfigure go through the same profile-resolution and tool-registration
logic. Daytona is excluded from reconfiguration since it requires a fundamentally different
runtime type.

## Execution

### Single turn: `run_prompt()`

Runs one agent turn and collects events into a `RunResult`:

1. If given a `RunState` (resume case), restores session state and extracts pending tool calls
2. Calls `agent.run_turn()`, optionally with pending tool calls and approval overrides
3. Streams events, tracking text output, usage, and status
4. On interruption: captures `RunState` snapshot with pending approval items
5. Returns `RunResult` with status one of: `completed`, `cancelled`, `interrupted`, `error`

### Persistent turn: `run_prompt_stored()`

Wraps `run_prompt()` with automatic SQLite-backed state management:

- **Interrupt** → saves `RunState` to store (can resume later)
- **Complete/cancel/error** → deletes any persisted state
- **Resume** (`prompt=None`) → loads saved `RunState` from store

### Background dispatch: `dispatch_prompt()`

Fires `run_prompt()` in an `asyncio.Task` and returns an `AgentHandle` with
`cancel()`, `done()`, `wait()`, and `status`. Used by the conductor for
multi-agent orchestration.

## Interrupt/Resume State Model

When a tool call requires approval and the user hasn't pre-approved it, the
turn is **interrupted**. `RunState` captures everything needed to resume:

- `session_id` — logical session identity
- `system_prompt` + `history` — full conversation (deep-copied to avoid aliasing)
- Token/cost counters — so resumed sessions report accurate cumulative usage
- `pending_approvals` — list of `ToolApprovalItem` (tool call ID, name, args)

`RunState` is fully JSON-serializable via `to_dict()` / `from_dict()`.
`SqliteRunStore` persists it as JSON text in a `run_states` table, keyed by run ID.

The resume flow:

1. `run_prompt_stored(prompt=None)` loads saved `RunState` from SQLite
2. `run_prompt()` calls `runtime.restore_state(state)` to replay the snapshot onto the live runtime
3. Pending tool calls are re-submitted to `agent.run_turn()` with the user's approval decisions

## Reconfiguration

`reconfigure_runtime()` supports hot-swapping the capability profile on a live
`LocalRuntime` (powers the `/mode` command). It rebuilds the tool registry through
the same `build_runtime_registry()` path used during construction, then swaps the
registry, options, and profile name on the runtime in-place.

Daytona is excluded — switching to/from Daytona requires a full runtime restart
since it's a fundamentally different execution backend.
