## Observability Benchmarks

These scripts provide lightweight, repeatable load tests for the SQLite telemetry backend.
They exercise the real write path in `TelemetryStorage`:

- `create_session`
- `create_turn`
- `increment_session_tool_calls`
- `record_tool_execution`
- `end_turn`
- `update_session`

Use these for trend tracking and regression checks, not as production SLO guarantees.

Telemetry writes are now best-effort: transient SQLite lock issues should not fail agent turns.
When pressure is high, expect possible dropped telemetry writes rather than agent failures.

### Scripts

- `sqlite_thread_bench.py`: concurrent writer load using threads in a single process.
- `sqlite_process_bench.py`: concurrent writer load using multiple OS processes.

### Usage

From repo root:

```bash
python rho_agent/observability/benchmarks/sqlite_thread_bench.py
python rho_agent/observability/benchmarks/sqlite_process_bench.py
```

Both scripts write to `/tmp` by default and print:

- total write operations
- duration
- operations/second
- count of SQLite `OperationalError` write failures

### Caveats

- Results depend heavily on machine, filesystem, and background load.
- These are short synthetic runs, not soak tests.
- They currently focus on writer concurrency; dashboard read pressure is not mixed in.
- Use relative comparisons over time (before/after code changes), not absolute thresholds.
