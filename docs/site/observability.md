---
title: Observability
description: Session telemetry, token tracking, tool metrics, dashboard, and monitor.
order: 9
---

Observability tracks agent sessions, per-turn token usage, and tool execution metrics. It is optional and enabled when `team_id` and `project_id` are provided (via CLI flags, environment variables, or config file).

## Enabling observability

### Via CLI flags

```bash
rho-agent main --team-id acme --project-id incident-response
```

### Via environment variables

```bash
export RHO_AGENT_TEAM_ID=acme
export RHO_AGENT_PROJECT_ID=incident-response
```

### Via configuration file

Create `~/.config/rho-agent/observability.yaml` (or point to a custom path with `--observability-config`):

```yaml
observability:
  enabled: true
  tenant:
    team_id: acme
    project_id: incident-response
  backend:
    type: sqlite
    sqlite:
      path: ~/.config/rho-agent/telemetry.db
  capture:
    traces: true
    metrics: true
    tool_arguments: true
    tool_results: false
```

### Via runtime API

```python
RuntimeOptions(
    team_id="acme",
    project_id="incident-response",
    telemetry_metadata={"job_id": "job-123", "environment": "staging"},
)
```

## What's tracked

### Sessions

Each agent run is a session. Tracked fields include:

- Session ID, team ID, project ID
- Model name and capability profile
- Start and end timestamps
- Final status (`completed`, `error`, `cancelled`)
- Total input, output, and reasoning tokens
- Turn count and total tool calls
- Custom metadata from `telemetry_metadata`

### Turns

Each user-agent exchange within a session is a turn:

- Turn index (0-based)
- User input text
- Start and end timestamps
- Input, output, and reasoning token counts
- Number of tool calls in the turn

### Tool executions

Each tool call within a turn:

- Tool name
- Start and end timestamps, duration
- Success or failure
- Arguments (when `capture.tool_arguments` is enabled)
- Result (when `capture.tool_results` is enabled — disabled by default as results can be large)

## Configuration options

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable or disable observability |
| `tenant.team_id` | string | — | Team identifier (required) |
| `tenant.project_id` | string | — | Project identifier (required) |
| `backend.type` | string | `sqlite` | `sqlite` or `otlp` |
| `backend.sqlite.path` | string | `~/.config/rho-agent/telemetry.db` | SQLite database path |
| `backend.otlp.endpoint` | string | `http://localhost:4317` | OTLP gRPC endpoint |
| `backend.otlp.insecure` | bool | `false` | Skip TLS verification |
| `backend.otlp.headers` | dict | `{}` | Custom headers for OTLP export |
| `capture.traces` | bool | `true` | Capture trace data |
| `capture.metrics` | bool | `true` | Capture metric data |
| `capture.tool_arguments` | bool | `true` | Record tool call arguments |
| `capture.tool_results` | bool | `false` | Record tool call results |

## Dashboard

The Streamlit dashboard provides a visual interface for browsing telemetry data.

```bash
rho-agent dashboard
rho-agent dashboard --port 9090 --db /path/to/telemetry.db
```

Features:

- Session history table with status, model, and profile filters
- Session detail view with turn-by-turn breakdown and tool execution timeline
- Token usage analytics by team, project, and time period
- Tool execution statistics: call frequency, success rate, and average duration

## Monitor

The monitor is an interactive command center that combines telemetry browsing with live agent management.

```bash
rho-agent monitor
rho-agent monitor --db /path/to/telemetry.db --limit 50
```

From the monitor you can:

- Browse sessions and view detailed turn-by-turn breakdowns
- List, pause, resume, and cancel running agents
- Inject directives into running interactive agents
- Connect multiple running agents for cross-context collaboration
- View an overview combining running agents with active telemetry sessions

See [CLI Reference](cli-reference/) for the full list of monitor commands.

## Storage API

The `TelemetryStorage` class provides programmatic access to telemetry data:

```python
from rho_agent.observability.storage import TelemetryStorage

storage = TelemetryStorage("~/.config/rho-agent/telemetry.db", read_only=True)

# List recent sessions
sessions = storage.list_sessions(limit=20)

# Get session detail with turns and tool executions
detail = storage.get_session(session_id)

# Analytics
cost = storage.get_cost_summary(team_id="acme", project_id="logs", days=30)
tools = storage.get_tool_stats(team_id="acme", project_id="logs", days=30)
```
