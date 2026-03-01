"""Convert rho-agent trace.jsonl to ATIF (Agent Trajectory Interchange Format).

Produces plain dicts matching the ATIF schema (no Pydantic dependency).
See Harbor RFC 0001-trajectory-format.md for the full spec.

NOTE: logprobs, prompt_token_ids, and completion_token_ids fields are reserved
for future RL support — capturing them requires API-level changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = "ATIF-v1.6"

# Event types we skip — they don't map to ATIF steps.
_SKIP_TYPES = {"run_start", "run_end", "tool_start", "tool_end", "tool_blocked", "compact", "llm_start"}


def trace_to_atif(
    trace_path: str | Path,
    *,
    session_id: str,
    agent_name: str = "rho-agent",
    agent_version: str = "0.1.0",
    model_name: str | None = None,
) -> dict[str, Any]:
    """Convert a trace.jsonl file into an ATIF trajectory dict.

    Args:
        trace_path: Path to the trace.jsonl file.
        session_id: Unique identifier for this session.
        agent_name: Agent name for the ATIF agent field.
        agent_version: Agent version string.
        model_name: Default model name for the trajectory.

    Returns:
        A dict matching the ATIF trajectory schema.
    """
    events = _read_events(trace_path)
    return _build_trajectory(
        events,
        session_id=session_id,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
    )


def _read_events(trace_path: str | Path) -> list[dict[str, Any]]:
    """Read all events from a trace.jsonl file."""
    events: list[dict[str, Any]] = []
    with open(trace_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def _build_trajectory(
    events: list[dict[str, Any]],
    *,
    session_id: str,
    agent_name: str,
    agent_version: str,
    model_name: str | None,
) -> dict[str, Any]:
    """Build an ATIF trajectory dict from a list of trace events."""
    steps: list[dict[str, Any]] = []
    final_metrics: dict[str, Any] | None = None
    # Collect llm_end events to pair with agent steps.
    pending_llm_end: dict[str, Any] | None = None

    i = 0
    while i < len(events):
        event = events[i]
        etype = event.get("type")

        if etype == "usage":
            final_metrics = _build_final_metrics(event, len(steps))
            i += 1
            continue

        if etype == "llm_end":
            pending_llm_end = event
            i += 1
            continue

        if etype in _SKIP_TYPES:
            i += 1
            continue

        if etype != "message":
            i += 1
            continue

        role = event.get("role")

        if role in ("user", "system"):
            steps.append(_make_simple_step(
                len(steps) + 1,
                source=role,
                message=event.get("content", ""),
                timestamp=event.get("ts"),
            ))
            i += 1
            continue

        if role == "assistant":
            step, i = _build_agent_step(events, i, len(steps) + 1, pending_llm_end)
            steps.append(step)
            pending_llm_end = None
            continue

        # tool messages outside of an agent step context — shouldn't happen in
        # well-formed traces, but skip gracefully.
        i += 1

    trajectory: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": session_id,
        "agent": {
            "name": agent_name,
            "version": agent_version,
        },
        "steps": steps,
    }
    if model_name:
        trajectory["agent"]["model_name"] = model_name
    if final_metrics:
        trajectory["final_metrics"] = final_metrics
    return trajectory


def _make_simple_step(
    step_id: int,
    *,
    source: str,
    message: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": source,
        "message": message,
    }
    if timestamp:
        step["timestamp"] = timestamp
    return step


def _build_agent_step(
    events: list[dict[str, Any]],
    start_idx: int,
    step_id: int,
    pending_llm_end: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    """Build an agent step from an assistant message and subsequent tool results.

    An agent step groups:
      - The assistant message (text and/or tool_calls)
      - Any immediately following tool-role messages (observation results)
      - Any llm_end event that preceded the assistant message (metrics)

    Returns:
        (step_dict, next_index) — the step and the index to continue from.
    """
    event = events[start_idx]
    i = start_idx + 1

    # Build the message text
    message = event.get("content") or ""

    # Build tool_calls
    raw_tool_calls = event.get("tool_calls") or []
    tool_calls = [
        {
            "tool_call_id": tc["id"],
            "function_name": tc["function"]["name"],
            "arguments": _parse_arguments(tc["function"]["arguments"]),
        }
        for tc in raw_tool_calls
    ]

    # Collect subsequent tool results and llm_end events
    observation_results: list[dict[str, Any]] = []
    metrics: dict[str, Any] | None = None

    # If we had a pending llm_end before the assistant message, use it
    if pending_llm_end:
        metrics = _build_step_metrics(pending_llm_end)

    while i < len(events):
        next_event = events[i]
        next_type = next_event.get("type")

        if next_type in _SKIP_TYPES:
            i += 1
            continue

        if next_type == "message" and next_event.get("role") == "tool":
            observation_results.append({
                "source_call_id": next_event.get("tool_call_id"),
                "content": next_event.get("content", ""),
            })
            i += 1
            continue

        # Any other event (llm_end, next message, usage, etc.) belongs to the
        # outer loop — don't consume it here.
        break

    step: dict[str, Any] = {
        "step_id": step_id,
        "source": "agent",
        "message": message,
    }
    if event.get("ts"):
        step["timestamp"] = event["ts"]
    if tool_calls:
        step["tool_calls"] = tool_calls
    if observation_results:
        step["observation"] = {"results": observation_results}
    if metrics:  # non-empty dict
        step["metrics"] = metrics

    return step, i


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse tool call arguments from string or dict form."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": raw}
    return {}


def _build_step_metrics(llm_end: dict[str, Any]) -> dict[str, Any]:
    """Build ATIF Metrics from an llm_end event."""
    metrics: dict[str, Any] = {}
    if "input_tokens" in llm_end:
        metrics["prompt_tokens"] = llm_end["input_tokens"]
    if "output_tokens" in llm_end:
        metrics["completion_tokens"] = llm_end["output_tokens"]
    if "cache_read_tokens" in llm_end:
        metrics["cached_tokens"] = llm_end["cache_read_tokens"]
    if "cost_usd" in llm_end:
        metrics["cost_usd"] = llm_end["cost_usd"]
    return metrics


def _build_final_metrics(usage: dict[str, Any], total_steps: int) -> dict[str, Any]:
    """Build ATIF FinalMetrics from a usage summary event."""
    fm: dict[str, Any] = {"total_steps": total_steps}
    if "input_tokens" in usage:
        fm["total_prompt_tokens"] = usage["input_tokens"]
    if "output_tokens" in usage:
        fm["total_completion_tokens"] = usage["output_tokens"]
    if "cached_tokens" in usage:
        fm["total_cached_tokens"] = usage["cached_tokens"]
    if "cost_usd" in usage:
        fm["total_cost_usd"] = usage["cost_usd"]
    return fm
