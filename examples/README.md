# Examples

Runnable examples demonstrating the three main patterns for using the `rho_agent.runtime` API programmatically.

## Basic Single-Agent

[`basic_agent.py`](basic_agent.py) — The simplest starting point. Creates a runtime, sends a prompt, and collects the result.

```bash
uv run python examples/basic_agent.py ~/some/project "Summarize the error handling"
```

Key API: `create_runtime()` → `run_prompt()` → read `result.text`

## Parallel Multi-Agent Dispatch

[`log_debugger/`](log_debugger/) — Dispatches multiple read-only agents in parallel, each analyzing a different log file. Results are collected into a consolidated JSON report.

```bash
uv run python examples/log_debugger/run.py --demo --output report.json
```

Key API: `create_runtime()` per agent → `dispatch_prompt()` → `handle.wait()` to collect results concurrently

## Streaming with Callbacks

[`sql_explorer/`](sql_explorer/) — A Streamlit app that streams agent tool calls and text to a chat UI in real-time.

```bash
python examples/sql_explorer/seed_database.py   # one-time setup
uv run streamlit run examples/sql_explorer/app.py
```

Key API: `run_prompt(runtime, prompt, on_event=callback)` for real-time event handling
