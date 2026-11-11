"""State — pure data object holding the accumulating record of a conversation.

State is the trajectory: messages, tool calls/results, usage, cost.
The single source of truth. Observable, serializable, inspectable without a Session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StateObserver(Protocol):
    """Observer notified on every state mutation."""

    def on_event(self, event: dict[str, Any]) -> None:
        """Called with each trace event dict."""
        ...


@dataclass
class State:
    """Pure conversation trajectory in the Agent/State/Session decomposition.

    The single source of truth for a conversation: messages (OpenAI chat format),
    cumulative token usage/cost, and run status. State is the trace — there is no
    separate observability data store.

    Key properties:
    - **Serializable**: ``to_jsonl()`` / ``from_jsonl()`` for persistence and replay.
    - **Incremental writes**: When ``trace_path`` is set, every event is appended to
      ``trace.jsonl`` immediately (crash-safe, no explicit save step).
    - **Observable**: Attach ``StateObserver`` instances for live export to external
      systems (Postgres, OTel). Observers are fire-and-forget side channels.
    - **Inspectable without a Session**: Load a trace file and analyze offline.

    State does NOT hold the system prompt or tools — those belong to Agent.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int | float] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
        }
    )
    status: str = "created"
    run_count: int = 0

    # Optional trace file for incremental writing
    trace_path: Path | None = field(default=None, repr=False)
    _observers: list[StateObserver] = field(default_factory=list, repr=False)

    # --- Observer management ---

    def add_observer(self, observer: StateObserver) -> None:
        """Attach an observer for live export."""
        self._observers.append(observer)

    def remove_observer(self, observer: StateObserver) -> None:
        """Detach an observer."""
        self._observers.remove(observer)

    # --- Event recording ---

    def _emit(self, event: dict[str, Any]) -> None:
        """Write event to trace file and notify observers."""
        if "ts" not in event:
            event["ts"] = datetime.now(UTC).isoformat()
        if self.trace_path is not None:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
                f.flush()
        for obs in self._observers:
            try:
                obs.on_event(event)
            except Exception:
                pass  # observers are fire-and-forget

    # --- Message manipulation ---

    def add_user_message(self, content: str) -> None:
        """Add a user message."""
        msg = {"role": "user", "content": content}
        self.messages.append(msg)
        self._emit({"type": "message", **msg})

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message."""
        msg = {"role": "assistant", "content": content}
        self.messages.append(msg)
        self._emit({"type": "message", **msg})

    def add_assistant_tool_calls(
        self, tool_calls: list[dict[str, Any]], content: str | None = None
    ) -> None:
        """Add assistant message with tool calls (and any text that came with them)."""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        }
        self.messages.append(msg)
        self._emit({"type": "message", **msg})

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Add a single tool result message."""
        msg = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        self.messages.append(msg)
        self._emit({"type": "message", **msg})

    def add_system_message(self, content: str) -> None:
        """Add a system message."""
        msg = {"role": "system", "content": content}
        self.messages.append(msg)
        self._emit({"type": "message", **msg})

    # --- Usage tracking ---

    def update_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate token usage and cost."""
        self.usage["input_tokens"] += input_tokens
        self.usage["output_tokens"] += output_tokens
        self.usage["cached_tokens"] += cached_tokens
        self.usage["reasoning_tokens"] += reasoning_tokens
        self.usage["cost_usd"] += cost_usd

    # --- Context management ---

    def get_messages(self) -> list[dict[str, Any]]:
        """Get messages in API format (copy)."""
        return list(self.messages)

    def replace_with_summary(
        self, summary: str, recent_user_messages: list[str] | None = None
    ) -> None:
        """Replace history with a compacted summary."""
        new_messages: list[dict[str, Any]] = []
        if recent_user_messages:
            for msg in recent_user_messages:
                new_messages.append({"role": "user", "content": msg})
        new_messages.append({"role": "user", "content": summary})
        self.replace_messages(new_messages)

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace the whole history and record the replacement in the trace.

        The ``history_replaced`` event carries the new messages so that
        ``from_jsonl`` can rebuild post-compaction state on resume.
        """
        self.messages = [dict(m) for m in messages]
        self._emit({"type": "history_replaced", "messages": [dict(m) for m in self.messages]})

    def pending_tool_call_ids(self) -> list[str]:
        """IDs of assistant tool calls that have no tool result yet."""
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        pending: list[str] = []
        for m in self.messages:
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in answered:
                    pending.append(tc_id)
        return pending

    def close_pending_tool_calls(self, reason: str) -> int:
        """Give every unanswered tool call a result so the history stays valid.

        Chat APIs reject a history where an assistant tool call has no matching
        tool message. Results are inserted directly after the calling assistant
        message (and any results already there), not appended at the end.
        """
        pending = set(self.pending_tool_call_ids())
        if not pending:
            return 0
        repaired: list[dict[str, Any]] = []
        closed = 0
        i = 0
        while i < len(self.messages):
            m = self.messages[i]
            repaired.append(m)
            i += 1
            calls = m.get("tool_calls") or []
            if not calls:
                continue
            while i < len(self.messages) and self.messages[i].get("role") == "tool":
                repaired.append(self.messages[i])
                i += 1
            for tc in calls:
                if tc.get("id") in pending:
                    result = {"role": "tool", "tool_call_id": tc["id"], "content": reason}
                    repaired.append(result)
                    self._emit({"type": "message", **result})
                    closed += 1
        appended_only = repaired[: len(self.messages)] == self.messages
        self.messages = repaired
        if not appended_only:
            # Results were inserted mid-history; record the exact order for replay.
            self._emit({"type": "history_replaced", "messages": [dict(m) for m in self.messages]})
        return closed

    def get_user_messages(self) -> list[str]:
        """Extract all user messages from history."""
        return [m["content"] for m in self.messages if m.get("role") == "user" and m.get("content")]

    def estimate_tokens(self, system_prompt: str = "") -> int:
        """Rough estimate of tokens in history (4 chars ~ 1 token)."""
        total_chars = len(system_prompt)
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                total_chars += sum(len(str(c)) for c in content)
            if m.get("tool_calls"):
                total_chars += len(str(m["tool_calls"]))
        return total_chars // 4

    # --- Serialization ---

    def to_jsonl(self) -> bytes:
        """Serialize state to JSONL bytes (message events)."""
        lines = []
        for msg in self.messages:
            event = {"type": "message", **msg}
            lines.append(json.dumps(event, default=str))
        # Append usage summary
        lines.append(
            json.dumps(
                {
                    "type": "usage",
                    **self.usage,
                    "status": self.status,
                    "run_count": self.run_count,
                }
            )
        )
        return ("\n".join(lines) + "\n").encode("utf-8")

    @classmethod
    def from_jsonl(cls, data: bytes) -> State:
        """Deserialize state from JSONL bytes.

        Accepts both a live ``trace.jsonl`` (message, llm_end, history_replaced,
        run_start, run_end events) and the compact ``to_jsonl()`` form (message
        events followed by one ``usage`` summary).
        """
        state = cls()
        saw_run_end = False
        for line in data.decode("utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final line from a killed process
            event_type = event.get("type")
            if event_type == "message":
                msg = {k: v for k, v in event.items() if k != "type" and k != "ts"}
                state.messages.append(msg)
            elif event_type == "history_replaced":
                state.messages = [dict(m) for m in event.get("messages", [])]
            elif event_type == "llm_end":
                state.usage["input_tokens"] += event.get("input_tokens", 0) or 0
                state.usage["output_tokens"] += event.get("output_tokens", 0) or 0
                state.usage["cached_tokens"] += event.get("cache_read_tokens", 0) or 0
                state.usage["reasoning_tokens"] += event.get("reasoning_tokens", 0) or 0
                state.usage["cost_usd"] += event.get("cost_usd", 0.0) or 0.0
            elif event_type == "run_start":
                state.run_count += 1
            elif event_type == "run_end":
                state.status = event.get("status", "completed")
                saw_run_end = True
            elif event_type == "usage":
                state.usage["input_tokens"] = event.get("input_tokens", 0)
                state.usage["output_tokens"] = event.get("output_tokens", 0)
                state.usage["cached_tokens"] = event.get("cached_tokens", 0)
                state.usage["reasoning_tokens"] = event.get("reasoning_tokens", 0)
                state.usage["cost_usd"] = event.get("cost_usd", 0.0)
                state.status = event.get("status", "completed")
                state.run_count = event.get("run_count", 0)
                saw_run_end = True
        if state.run_count and not saw_run_end:
            state.status = "interrupted"  # process died mid-run
        # A killed or cancelled run can leave tool calls without results.
        state.close_pending_tool_calls("Interrupted before this tool call finished.")
        return state
