# Log Debugger — Parallel Multi-Agent Example

Dispatches read-only debug agents in parallel across multiple working directories, each analyzing a different log file from a failed process. Results are collected into a single consolidated JSON report.

## How it works

1. You define a list of **incidents** — each is a `(working_dir, log_file, service_name)` tuple
2. The orchestrator creates one `readonly` `Agent`/`Session` per incident, pointed at that directory
3. All agents run concurrently via `asyncio.gather()`
4. Each agent reads its log file, diagnoses the root cause, and returns structured JSON
5. Results are merged into a single report with per-incident diagnoses and a summary

## Setup

Requires an OpenAI-compatible API key. Set it via environment variable or `.env` file in the project root:

```bash
export OPENAI_API_KEY=sk-...
# or
echo 'OPENAI_API_KEY=sk-...' >> .env
```

To use a non-OpenAI provider, set `OPENAI_BASE_URL` as well:

```bash
export OPENAI_BASE_URL=https://api.together.xyz/v1
export OPENAI_API_KEY=your-key
```

## Usage

```bash
# Demo mode — creates fake log dirs under /tmp with realistic failures
uv run python examples/log_debugger/run.py --demo --output report.json

# Real incidents — one --incident flag per failed service
uv run python examples/log_debugger/run.py \
    --incident /var/log/myapp:app.log:myapp-api \
    --incident /var/log/worker:worker.log:celery-worker \
    --output report.json

# Use a different model
uv run python examples/log_debugger/run.py --demo --model gpt-5
```

## Monitoring

Sessions are persisted to disk so you can observe agents in real time from a second terminal:

```bash
# Terminal 1 — start the monitor
rho-agent monitor /tmp/rho-agent-log-debug-sessions

# Terminal 2 — run the demo
uv run python examples/log_debugger/run.py --demo
```

Inside the monitor, use `ps` to list sessions, `watch <service-name>` to tail trace events, or `cancel <service-name>` to stop an agent. Type `help` for the full command list.

Use `--sessions-dir` to customize where session state is written.

The `--incident` format is `DIR:LOGFILE:SERVICE` where:
- `DIR` — working directory the agent operates in
- `LOGFILE` — filename of the log to analyze (relative to DIR)
- `SERVICE` — human-readable service name for the report

## Demo incidents

`--demo` generates five simulated failures:

| Service | Failure | Category |
|---------|---------|----------|
| payment-svc | Postgres connection lost after i/o timeout | network_error |
| auth-svc | JVM OOM with heap exhaustion | oom |
| notif-worker | SMTP relay unreachable, circuit breaker open | dependency_failure |
| etl-daily | Disk full on warehouse node during load stage | disk_full |
| api-gw | Cascading upstream failures (3 of 4 backends down) | dependency_failure |

## Output

The consolidated report (`debug_report.json`) contains:

```json
{
  "generated_at": "2025-06-15T12:00:00Z",
  "total_incidents": 5,
  "diagnosed": 5,
  "failed": 0,
  "summary": {
    "by_severity": {"critical": 3, "high": 2},
    "by_category": {"network_error": 1, "oom": 1, ...}
  },
  "reports": [ ... ],
  "errors": null
}
```

Each report entry includes `root_cause`, `category`, `severity`, `timeline`, `evidence`, and `recommendation`.

## Files

- `debug.md` — System prompt for the read-only debug agents
- `run.py` — Orchestrator that dispatches agents and collects the report
