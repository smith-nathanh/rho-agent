---
title: Observability
description: Session telemetry, token tracking, tool metrics, dashboard, and monitor.
order: 9
---

Observability tracks agent sessions, per-turn token usage, and tool execution metrics. It is optional and enabled when `team_id` and `project_id` are provided (via CLI flags, environment variables, or config file).

Two storage backends are available:

- **SQLite** (default) — zero-config, single-file, works out of the box
- **PostgreSQL** (opt-in) — concurrent multi-user writes, cross-node agent registry, and real-time event streaming via LISTEN/NOTIFY

## Enabling observability

### Via CLI flags

```bash
rho-agent --team-id acme --project-id incident-response
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

## Backends

### SQLite (default)

No additional dependencies. Data is stored at `~/.config/rho-agent/telemetry.db` by default.

### PostgreSQL

Install the extra:

```bash
uv sync --extra obs-postgres    # or: pip install 'rho-agent[obs-postgres]'
```

Then configure via environment variable:

```bash
export RHO_AGENT_OBSERVABILITY_DSN="postgresql://rho:secret@db-host:5432/rho_observability"
```

Or via config file:

```yaml
observability:
  backend:
    type: postgres
    postgres:
      dsn: "postgresql://rho:secret@db-host:5432/rho_observability"
      max_connections: 10
```

Tables are created automatically on first connect. The schema includes LISTEN/NOTIFY triggers for real-time event streaming and GIN indexes on JSONB columns for label queries.

The Postgres backend also provides:

- **Agent registry** — running agents register in a shared `agent_registry` table, enabling `rho-agent ps` and `rho-agent kill` across nodes
- **Heartbeat** — agents send a heartbeat every 15 seconds; agents missing 3 heartbeats (45s) are considered stale
- **Signal queue** — `kill`, `pause`, `resume`, and `directive` commands are delivered via a `signal_queue` table instead of filesystem signals

## Labels

Labels tag sessions with arbitrary metadata (cluster name, team, environment, job ID, etc.) without schema changes. Labels are stored in the session's `metadata` JSON column.

### Via environment variable

```bash
export RHO_AGENT_LABELS="cluster=hpc-west,team=ml-infra"
```

### Via config file

```yaml
observability:
  labels:
    cluster: hpc-west
    team: ml-infra
```

YAML labels take precedence over environment variable labels when both are set.

Labels are queryable via `json_extract(metadata, '$.labels.cluster')` (SQLite) or `metadata->'labels'->>'cluster'` (Postgres).

## What's tracked

### Sessions

Each agent run is a session. Tracked fields include:

- Session ID, team ID, project ID
- Model name and capability profile
- Start and end timestamps
- Final status (`completed`, `error`, `cancelled`)
- Total input, output, and reasoning tokens
- Turn count and total tool calls
- Custom metadata from `telemetry_metadata` and labels

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

## Configuration reference

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable or disable observability |
| `tenant.team_id` | string | — | Team identifier (required) |
| `tenant.project_id` | string | — | Project identifier (required) |
| `backend.type` | string | `sqlite` | `sqlite`, `postgres`, or `otlp` |
| `backend.sqlite.path` | string | `~/.config/rho-agent/telemetry.db` | SQLite database path |
| `backend.postgres.dsn` | string | — | PostgreSQL connection string |
| `backend.postgres.min_connections` | int | `2` | Minimum pool size |
| `backend.postgres.max_connections` | int | `10` | Maximum pool size |
| `backend.otlp.endpoint` | string | `http://localhost:4317` | OTLP gRPC endpoint |
| `backend.otlp.insecure` | bool | `false` | Skip TLS verification |
| `backend.otlp.headers` | dict | `{}` | Custom headers for OTLP export |
| `labels` | dict | `{}` | Key-value labels stored in session metadata |
| `capture.traces` | bool | `true` | Capture trace data |
| `capture.metrics` | bool | `true` | Capture metric data |
| `capture.tool_arguments` | bool | `true` | Record tool call arguments |
| `capture.tool_results` | bool | `false` | Record tool call results |

## Configuration examples

**Single user (default, unchanged):**

```yaml
observability:
  backend:
    type: sqlite
```

**Multi-user shared SQLite (HPC, POSIX shared filesystem):**

```yaml
observability:
  backend:
    type: sqlite
    sqlite:
      path: /shared/rho-agent/telemetry.db
  labels:
    cluster: hpc-west
```

**Postgres cluster mode:**

```yaml
observability:
  backend:
    type: postgres
    postgres:
      dsn: "postgresql://rho:secret@service-node:5432/rho_observability"
      max_connections: 10
  labels:
    cluster: hpc-west
    team: ml-infra
```

**Environment-only config:**

```bash
export RHO_AGENT_OBSERVABILITY_DSN=postgresql://rho:secret@service-node:5432/rho_observability
export RHO_AGENT_LABELS=cluster=hpc-west,team=ml-infra
export RHO_AGENT_TEAM_ID=acme
export RHO_AGENT_PROJECT_ID=ops
```

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

Both backends satisfy the `TelemetryStore` protocol. You can use the factory to get the right one based on config, or construct directly:

```python
from rho_agent.observability.storage.sqlite import TelemetryStorage

storage = TelemetryStorage("~/.config/rho-agent/telemetry.db", read_only=True)
```

```python
from rho_agent.observability.storage.postgres import PostgresTelemetryStore

storage = PostgresTelemetryStore("postgresql://rho:secret@localhost:5432/rho_observability")
```

Or use the factory:

```python
from rho_agent.observability.config import ObservabilityConfig
from rho_agent.observability.storage.factory import create_storage

config = ObservabilityConfig.load()
storage = create_storage(config)
```

All backends provide the same query interface:

```python
sessions = storage.list_sessions(team_id="acme", limit=20)
detail = storage.get_session_detail(session_id)
cost = storage.get_cost_summary(team_id="acme", project_id="logs", days=30)
tools = storage.get_tool_stats(team_id="acme", project_id="logs", days=30)
```
