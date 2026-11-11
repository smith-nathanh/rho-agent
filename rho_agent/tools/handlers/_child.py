"""Shared plumbing for tools that run a child agent session (delegate, agent tools).

A child session is linked to the session that spawned it so that:
- its trace is written under the parent's session directory;
- the parent's remaining cost and time budget caps the child;
- cancel, pause and approval reach the child;
- the child's token usage and cost are added to the parent's totals.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def child_session_kwargs(parent: Any | None, state: Any, *, share_client: bool) -> dict[str, Any]:
    """Constructor kwargs for a child Session, and wire its trace file.

    Args:
        parent: The spawning Session, or None when the tool is called outside one.
        state: The child's State; its ``trace_path`` is set when the parent has
            a session directory.
        share_client: Reuse the parent's model client. Only correct when the
            child uses the same model and endpoint as the parent.
    """
    kwargs: dict[str, Any] = {"state": state}
    if parent is None:
        return kwargs

    child_id = uuid.uuid4().hex[:8]
    kwargs["session_id"] = child_id
    if share_client:
        kwargs["client"] = parent._client

    parent_dir: Path | None = getattr(parent, "_session_dir", None)
    if parent_dir is not None:
        child_dir = parent_dir / "children" / child_id
        try:
            child_dir.mkdir(parents=True, exist_ok=True)
            state.trace_path = child_dir / "trace.jsonl"
            meta = {
                "parent_session_id": parent.id,
                "model": parent.agent.config.model,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }
            (child_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            kwargs["session_dir"] = child_dir
        except OSError:
            pass  # tracing a child is best-effort
    return kwargs


def link_child(parent: Any | None, child: Any, tool_name: str, instruction: str) -> None:
    """Apply the parent's controls and remaining budget to a child session."""
    if parent is None:
        return
    child.parent = parent
    child.approval_callback = parent.approval_callback
    child.cancel_check = parent._is_cancelled
    child.parallel_tool_calls = parent.parallel_tool_calls

    if parent.max_cost_usd is not None:
        spent = float(parent.state.usage.get("cost_usd", 0.0) or 0.0)
        child.max_cost_usd = max(parent.max_cost_usd - spent, 0.0)
    remaining = parent._wall_remaining()
    if remaining is not None:
        child.max_wall_seconds = max(remaining, 0.0)

    preview = instruction if len(instruction) <= 500 else instruction[:500] + "..."
    parent.state._emit(
        {
            "type": "child_start",
            "tool_name": tool_name,
            "child_session_id": child.id,
            "trace_path": str(child.state.trace_path) if child.state.trace_path else None,
            "instruction": preview,
        }
    )


def settle_child(parent: Any | None, child: Any, status: str) -> None:
    """Add a finished child's usage to the parent and record it in the trace."""
    if parent is None:
        return
    usage = getattr(getattr(child, "state", None), "usage", None)
    if not isinstance(usage, dict):
        usage = {}
    totals = {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_tokens": usage.get("cached_tokens", 0) or 0,
        "reasoning_tokens": usage.get("reasoning_tokens", 0) or 0,
        "cost_usd": usage.get("cost_usd", 0.0) or 0.0,
    }
    parent.state.update_usage(**totals)
    # Recorded as llm_end so the parent's totals replay correctly on resume.
    parent.state._emit(
        {
            "type": "llm_end",
            "model": child.agent.config.model,
            "purpose": "child",
            "child_session_id": child.id,
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cache_read_tokens": totals["cached_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "cost_usd": totals["cost_usd"],
        }
    )
    parent.state._emit({"type": "child_end", "child_session_id": child.id, "status": status})


def share_sandbox(parent: Any | None, child_agent: Any) -> None:
    """Point a child agent's remote tools at the parent's sandbox.

    Without this a child of a Daytona-backed parent would create a second,
    empty sandbox. The child does not own the sandbox and must not close it.
    """
    if parent is None:
        return
    manager = getattr(parent.agent, "sandbox_manager", None)
    if manager is None:
        return
    for handler in child_agent.registry._handlers.values():
        if hasattr(handler, "_manager"):
            handler._manager = manager
    child_agent._sandbox_manager = None
