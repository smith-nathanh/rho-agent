"""Threaded SQLite telemetry load benchmark.

Exercises TelemetryStorage write APIs under thread concurrency.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rho_agent.observability.context import TelemetryContext, ToolExecutionContext, TurnContext
from rho_agent.observability.storage.sqlite import TelemetryStorage


DB_PATH = Path("/tmp/rho_obs_bench.db")


def run_agent(agent_idx: int, turns: int, tools_per_turn: int) -> tuple[int, int]:
    """Return (successful_writes, sqlite_operational_errors)."""
    storage = TelemetryStorage(DB_PATH)
    errors = 0
    writes = 0

    session_id = f"agent-{agent_idx}-{uuid.uuid4()}"
    context = TelemetryContext(
        team_id="bench",
        project_id="obs",
        session_id=session_id,
        model="gpt-5-mini",
    )

    try:
        storage.create_session(context)
        writes += 1
    except sqlite3.OperationalError:
        errors += 1

    for turn_idx in range(1, turns + 1):
        turn_id = str(uuid.uuid4())
        turn = TurnContext(turn_id=turn_id, session_id=session_id, turn_index=turn_idx)

        try:
            storage.create_turn(turn, f"prompt {turn_idx}")
            writes += 1
        except sqlite3.OperationalError:
            errors += 1

        for tool_idx in range(tools_per_turn):
            execution = ToolExecutionContext(
                turn_id=turn_id,
                tool_name="bash",
                arguments={"cmd": f"echo {tool_idx}"},
                result="ok",
            )
            execution.end(success=True)

            try:
                storage.increment_session_tool_calls(session_id)
                writes += 1
            except sqlite3.OperationalError:
                errors += 1

            try:
                storage.record_tool_execution(execution)
                writes += 1
            except sqlite3.OperationalError:
                errors += 1

        turn.input_tokens = 100
        turn.output_tokens = 40
        turn.reasoning_tokens = 20
        turn.context_size = 2000
        turn.end()

        try:
            storage.end_turn(turn)
            writes += 1
        except sqlite3.OperationalError:
            errors += 1

    context.total_tool_calls = turns * tools_per_turn
    context.total_input_tokens = turns * 100
    context.total_output_tokens = turns * 40
    context.total_reasoning_tokens = turns * 20
    context.context_size = 2000
    context.end_session("completed")

    try:
        storage.update_session(context)
        writes += 1
    except sqlite3.OperationalError:
        errors += 1

    return writes, errors


def bench(concurrency: int, turns: int = 8, tools_per_turn: int = 3) -> dict[str, float | int]:
    start = time.perf_counter()
    total_writes = 0
    total_errors = 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(run_agent, i, turns, tools_per_turn)
            for i in range(concurrency)
        ]
        for future in as_completed(futures):
            writes, errors = future.result()
            total_writes += writes
            total_errors += errors

    duration_s = time.perf_counter() - start
    return {
        "concurrency": concurrency,
        "duration_s": duration_s,
        "writes": total_writes,
        "writes_per_s": total_writes / duration_s if duration_s else 0.0,
        "errors": total_errors,
    }


if __name__ == "__main__":
    if DB_PATH.exists():
        DB_PATH.unlink()
    TelemetryStorage(DB_PATH)

    for concurrency_level in (1, 2, 4, 8, 16, 24, 32, 48):
        print(bench(concurrency_level))
