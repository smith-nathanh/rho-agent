"""Convert rho-agent trace.jsonl to OpenTelemetry spans (OTLP).

Spans follow the OpenTelemetry GenAI semantic conventions
(https://github.com/open-telemetry/semantic-conventions-genai), which are still
in Development status:

- one ``invoke_agent`` span per ``session.run()`` (INTERNAL),
- one ``chat {model}`` span per model call (CLIENT),
- one ``execute_tool {tool}`` span per tool call (INTERNAL),
- a child session's ``invoke_agent`` span nested under the tool call that
  spawned it, with ``gen_ai.transfer.mode = return_to_caller``.

Each run is its own trace; ``gen_ai.conversation.id`` and ``session.id`` tie
the runs of a session together. Trace and span IDs are derived from the session
ID, the run number and the tool call ID, so exporting the same session twice
(or exporting it after the live observer sent it) gives the same IDs.

Message content (prompts, model output, tool arguments and results) is left out
unless ``include_content=True``.

The converter produces OTLP/JSON dicts and needs no dependencies. Sending with
``http/protobuf`` needs the ``otel`` extra (``opentelemetry-proto``).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

SCOPE_NAME = "rho_agent"
SERVICE_NAME = "rho-agent"
# Longest text kept in one content attribute part (only with include_content).
MAX_CONTENT_CHARS = 16_000

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_CODE_ERROR = 2

PROTOCOLS = ("http/protobuf", "http/json")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Run statuses reported as span errors. Others (cancelled, interrupted for an
# approval, max_turns, budget_exceeded) are deliberate stops and are only
# recorded in rho_agent.run.status. "unfinished" marks a run with no run_end
# (the process was killed).
_ERROR_RUN_STATUSES = {"error", "unfinished"}


# --- IDs ---------------------------------------------------------------------


def _digest(*parts: Any) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def trace_id_for(session_id: str, run_index: int) -> str:
    """Trace ID of a top-level session's run (32 hex chars)."""
    return _nonzero(_digest("rho-agent", "trace", session_id, run_index)[:32])


def span_id_for(*parts: Any) -> str:
    """Span ID derived from its identifying parts (16 hex chars)."""
    return _nonzero(_digest("rho-agent", "span", *parts)[:16])


def _nonzero(hex_id: str) -> str:
    # An all-zero ID is invalid in OTLP; vanishingly unlikely from SHA-256.
    return hex_id if int(hex_id, 16) else hex_id[:-1] + "1"


def run_span_id(session_id: str, run_index: int) -> str:
    return span_id_for("run", session_id, run_index)


def tool_span_id(session_id: str, run_index: int, tool_call_id: str) -> str:
    return span_id_for("tool", session_id, run_index, tool_call_id)


# --- Attribute encoding --------------------------------------------------------


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return {"arrayValue": {"values": [{"stringValue": v} for v in value]}}
    # Structured values are sent as JSON strings, as the OTel SDKs do.
    return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}


def encode_attributes(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    """Encode a dict as OTLP/JSON KeyValues, skipping None values."""
    return [{"key": k, "value": _any_value(v)} for k, v in attrs.items() if v is not None]


def decode_attributes(encoded: list[dict[str, Any]]) -> dict[str, Any]:
    """Inverse of :func:`encode_attributes`, for tests and inspection."""
    out: dict[str, Any] = {}
    for kv in encoded:
        v = kv["value"]
        if "intValue" in v:
            out[kv["key"]] = int(v["intValue"])
        elif "arrayValue" in v:
            out[kv["key"]] = [x.get("stringValue") for x in v["arrayValue"]["values"]]
        else:
            out[kv["key"]] = next(iter(v.values()))
    return out


# --- Time --------------------------------------------------------------------


def _ts_ns(ts: Any) -> int | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


# --- Content -------------------------------------------------------------------


def _clip(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[:MAX_CONTENT_CHARS] + f"...[{len(text) - MAX_CONTENT_CHARS} chars truncated]"


def _text_parts(content: Any) -> list[dict[str, Any]]:
    if content is None or content == "":
        return []
    if isinstance(content, str):
        return [{"type": "text", "content": _clip(content)}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                parts.append({"type": "text", "content": "[image]"})
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append({"type": "text", "content": _clip(str(part.get("text") or ""))})
            else:
                parts.append({"type": "text", "content": _clip(str(part))})
        return parts
    return [{"type": "text", "content": _clip(str(content))}]


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def to_genai_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Convert a rho-agent (OpenAI chat format) message to a GenAI semconv message."""
    role = msg.get("role") or "user"
    if role == "tool":
        content = msg.get("content")
        return {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": msg.get("tool_call_id"),
                    "response": _clip(content if isinstance(content, str) else str(content)),
                }
            ],
        }
    parts = _text_parts(msg.get("content"))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        parts.append(
            {
                "type": "tool_call",
                "id": tc.get("id"),
                "name": fn.get("name"),
                "arguments": _parse_args(fn.get("arguments")),
            }
        )
    return {"role": role, "parts": parts}


# --- Span building -------------------------------------------------------------


@dataclass
class ChildLink:
    """Where a child session's run attaches in its parent's trace."""

    child_session_id: str
    tool_name: str | None
    trace_id: str
    parent_span_id: str
    trace_path: str | None = None


@dataclass
class _Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_ns: int
    attrs: dict[str, Any] = field(default_factory=dict)
    end_ns: int | None = None
    error_type: str | None = None
    status_message: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_otlp(self) -> dict[str, Any]:
        end = max(self.end_ns if self.end_ns is not None else self.start_ns, self.start_ns)
        attrs = dict(self.attrs)
        if self.error_type:
            attrs["error.type"] = self.error_type
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(end),
            "attributes": encode_attributes(attrs),
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        if self.events:
            span["events"] = self.events
        if self.error_type:
            span["status"] = {"code": STATUS_CODE_ERROR, "message": self.status_message or ""}
        return span


ContextForRun = Callable[[int], tuple[str, str | None]]


class SessionSpanBuilder:
    """Turn one session's trace events into OTLP spans, one event at a time.

    Used by the offline converter (feed every event, then ``finish()``) and by
    the live observer (feed events as they happen). Finished spans are passed
    to ``emit`` as OTLP/JSON span dicts.

    Args:
        session_id: The session's ID.
        emit: Called with each finished span.
        context_for_run: Given a run number (1-based), returns the trace ID and
            parent span ID for that run's ``invoke_agent`` span. Defaults to a
            new trace per run with no parent (a top-level session).
        root_session_id: ID of the top-level session, recorded as ``session.id``.
        agent_name: ``gen_ai.agent.name`` for this session's spans.
        provider_name: ``gen_ai.provider.name`` for model calls.
        include_content: Record prompts, model output and tool arguments/results.
        run_index: Runs already recorded before the first event fed (live use
            on a resumed session).
        messages: Conversation history before the first event fed.
    """

    def __init__(
        self,
        session_id: str,
        emit: Callable[[dict[str, Any]], None],
        *,
        context_for_run: ContextForRun | None = None,
        root_session_id: str | None = None,
        agent_name: str | None = None,
        provider_name: str = "openai",
        include_content: bool = False,
        run_index: int = 0,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.root_session_id = root_session_id or session_id
        self.agent_name = agent_name
        self.provider_name = provider_name
        self.include_content = include_content
        self._emit = emit
        self._context_for_run = context_for_run or (lambda n: (trace_id_for(session_id, n), None))
        self.run_index = run_index
        self.child_links: dict[str, list[ChildLink]] = {}

        self._messages: list[dict[str, Any]] = [dict(m) for m in messages or []]
        self._compacted = False
        self._last_ns: int | None = None
        self._run: _Span | None = None
        self._run_usage: dict[str, float] = {}
        self._run_model: str | None = None
        self._run_last_text: str | None = None
        self._run_error: str | None = None
        self._chat: _Span | None = None  # model call in progress
        self._chat_done: _Span | None = None  # finished, waiting for its output message
        self._chat_count = 0
        self._outside_count = 0
        self._tools: dict[str, _Span] = {}
        self._tool_count = 0
        self._tool_results: dict[str, Any] = {}

    # --- public ---

    @property
    def current_trace_id(self) -> str | None:
        return self._run.trace_id if self._run else None

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        ts = _ts_ns(event.get("ts"))
        if ts is None:
            ts = self._last_ns if self._last_ns is not None else 0
        handler = getattr(self, f"_on_{etype}", None)
        if handler is not None:
            handler(event, ts)
        self._last_ns = max(ts, self._last_ns or 0)

    def finish(self) -> None:
        """Close whatever is still open (a killed process leaves runs unfinished)."""
        end = self._last_ns or 0
        self._flush_chat()
        if self._chat is not None:
            self._close_chat(end, error="interrupted", message="model call did not finish")
        self._close_tools(end)
        if self._run is not None:
            self._close_run(end, "unfinished")

    # --- span helpers ---

    def _common(self) -> dict[str, Any]:
        return {
            "gen_ai.conversation.id": self.session_id,
            "session.id": self.root_session_id,
            "gen_ai.agent.name": self.agent_name,
        }

    def _parent(self) -> tuple[str, str | None]:
        """Trace ID and parent span ID for a span opened now."""
        if self._run is not None:
            return self._run.trace_id, self._run.span_id
        # Outside a run (a manual /compact): a root span in the last run's trace.
        trace_id, _ = self._context_for_run(max(self.run_index, 1))
        return trace_id, None

    def _scope(self) -> tuple[Any, ...]:
        if self._run is not None:
            return (self.session_id, self.run_index)
        return (self.session_id, f"{self.run_index}+")

    def _send(self, span: _Span) -> None:
        self._emit(span.to_otlp())

    def _run_event(self, name: str, ts: int, attrs: dict[str, Any]) -> None:
        if self._run is None:
            return
        self._run.events.append(
            {"timeUnixNano": str(ts), "name": name, "attributes": encode_attributes(attrs)}
        )

    # --- runs ---

    def _on_run_start(self, event: dict[str, Any], ts: int) -> None:
        if self._run is not None:  # previous run never ended (process killed)
            self.finish()
        self.run_index += 1
        self._chat_count = 0
        self._tool_count = 0
        self._run_usage = {}
        self._run_model = None
        self._run_last_text = None
        self._run_error = None
        trace_id, parent = self._context_for_run(self.run_index)
        name = f"invoke_agent {self.agent_name}" if self.agent_name else "invoke_agent"
        attrs = {
            "gen_ai.operation.name": "invoke_agent",
            **self._common(),
            "rho_agent.run.index": self.run_index,
        }
        if self.include_content and event.get("prompt") is not None:
            prompt = event["prompt"]
            attrs["gen_ai.input.messages"] = [{"role": "user", "parts": _text_parts(prompt)}]
        self._run = _Span(
            trace_id=trace_id,
            span_id=run_span_id(self.session_id, self.run_index),
            parent_span_id=parent,
            name=name,
            kind=SPAN_KIND_INTERNAL,
            start_ns=ts,
            attrs=attrs,
        )

    def _on_run_end(self, event: dict[str, Any], ts: int) -> None:
        self._flush_chat()
        if self._chat is not None:
            self._close_chat(ts, error="interrupted", message="model call did not finish")
        self._close_tools(ts)
        if self._run is not None:
            self._close_run(ts, str(event.get("status") or "completed"))

    def _close_run(self, ts: int, status: str) -> None:
        run = self._run
        assert run is not None
        run.end_ns = ts
        run.attrs["rho_agent.run.status"] = status
        if self._run_model:
            run.attrs["gen_ai.request.model"] = self._run_model
        # Run totals (children included) go under rho_agent.*, not gen_ai.usage.*,
        # so backends that sum gen_ai.usage over a trace do not count them twice.
        for key, value in self._run_usage.items():
            if key == "cost_usd" and not value:
                continue  # no price known, not free
            run.attrs[f"rho_agent.usage.{key}"] = value
        if self.include_content and self._run_last_text:
            run.attrs["gen_ai.output.messages"] = [
                {"role": "assistant", "parts": _text_parts(self._run_last_text)}
            ]
        if status in _ERROR_RUN_STATUSES:
            run.error_type = self._run_error or status
            run.status_message = f"run {status}"
        self._run = None
        self._send(run)

    # --- messages ---

    def _on_message(self, event: dict[str, Any], ts: int) -> None:
        msg = {k: v for k, v in event.items() if k not in ("type", "ts")}
        role = msg.get("role")
        if self.include_content:
            self._messages.append(msg)
            if role == "tool":
                self._tool_results[str(msg.get("tool_call_id"))] = msg.get("content")
        if role == "assistant":
            if msg.get("content"):
                self._run_last_text = msg["content"]
            if self._chat_done is not None:
                if self.include_content:
                    out = to_genai_message(msg)
                    out["finish_reason"] = "tool_call" if msg.get("tool_calls") else "stop"
                    self._chat_done.attrs["gen_ai.output.messages"] = [out]
                self._flush_chat()

    def _on_history_replaced(self, event: dict[str, Any], ts: int) -> None:
        self._compacted = True
        if self.include_content:
            self._messages = [dict(m) for m in event.get("messages") or []]

    # --- model calls ---

    def _on_llm_start(self, event: dict[str, Any], ts: int) -> None:
        self._flush_chat()
        if self._chat is not None:  # a start with no end: superseded
            self._close_chat(ts, error="interrupted", message="model call did not finish")
        self._open_chat(event, ts)

    def _open_chat(self, event: dict[str, Any], ts: int) -> None:
        trace_id, parent = self._parent()
        if self._run is not None:
            self._chat_count += 1
            n = self._chat_count
        else:
            self._outside_count += 1
            n = self._outside_count
        model = event.get("model")
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self.provider_name,
            "gen_ai.request.model": model,
            **self._common(),
            "rho_agent.purpose": event.get("purpose"),
            "rho_agent.context_size": event.get("context_size"),
        }
        if self._compacted:
            attrs["gen_ai.conversation.compacted"] = True
        if self.include_content and event.get("purpose") is None and self._messages:
            attrs["gen_ai.input.messages"] = [to_genai_message(m) for m in self._messages]
        if model and self._run is not None and self._run_model is None:
            self._run_model = model
        self._chat = _Span(
            trace_id=trace_id,
            span_id=span_id_for("chat", *self._scope(), n),
            parent_span_id=parent,
            name=f"chat {model}" if model else "chat",
            kind=SPAN_KIND_CLIENT,
            start_ns=ts,
            attrs=attrs,
        )

    def _on_llm_end(self, event: dict[str, Any], ts: int) -> None:
        usage = {
            "input_tokens": event.get("input_tokens") or 0,
            "output_tokens": event.get("output_tokens") or 0,
            "cache_read_tokens": event.get("cache_read_tokens") or 0,
            "reasoning_tokens": event.get("reasoning_tokens") or 0,
            "cost_usd": float(event.get("cost_usd") or 0.0),
        }
        if self._run is not None:
            for k, v in usage.items():
                self._run_usage[k] = self._run_usage.get(k, 0) + v
        purpose = event.get("purpose")
        if purpose == "child":
            return  # a child session's usage roll-up; its own spans carry the calls
        self._flush_chat()
        if self._chat is None:
            # Older traces have no llm_start for compaction calls.
            self._open_chat(event, self._last_ns if self._last_ns is not None else ts)
        chat = self._chat
        assert chat is not None
        chat.attrs.update(
            {
                "gen_ai.usage.input_tokens": usage["input_tokens"],
                "gen_ai.usage.output_tokens": usage["output_tokens"],
            }
        )
        # rho records 0 when it has no price for the model. Leave cost out then,
        # so a backend with its own price table (Langfuse) does not show $0.
        if usage["cost_usd"]:
            chat.attrs["rho_agent.cost_usd"] = usage["cost_usd"]
            # Not in the semconv registry; read by Langfuse and OpenLLMetry.
            chat.attrs["gen_ai.usage.cost"] = usage["cost_usd"]
        if usage["cache_read_tokens"]:
            chat.attrs["gen_ai.usage.cache_read.input_tokens"] = usage["cache_read_tokens"]
        if usage["reasoning_tokens"]:
            chat.attrs["gen_ai.usage.reasoning.output_tokens"] = usage["reasoning_tokens"]
        chat.end_ns = ts
        self._chat = None
        if purpose:
            self._send(chat)  # compaction: the summary is not a conversation message
        else:
            self._chat_done = chat  # wait for the assistant message it produced

    def _on_llm_error(self, event: dict[str, Any], ts: int) -> None:
        kind = str(event.get("kind") or "error")
        if self._run is not None:
            self._run_error = kind
        if self._chat is not None:
            message = str(event.get("error") or "") if self.include_content else kind
            self._close_chat(ts, error=kind, message=message[:1000])

    def _on_compact_failed(self, event: dict[str, Any], ts: int) -> None:
        if self._chat is not None and self._chat.attrs.get("rho_agent.purpose") == "compaction":
            message = str(event.get("error") or "") if self.include_content else ""
            self._close_chat(ts, error="compaction_failed", message=message[:1000])
        self._run_event("rho_agent.compact_failed", ts, {"trigger": event.get("trigger")})

    def _close_chat(self, ts: int, *, error: str, message: str) -> None:
        chat = self._chat
        assert chat is not None
        chat.end_ns = ts
        chat.error_type = error
        chat.status_message = message
        self._chat = None
        self._send(chat)

    def _flush_chat(self) -> None:
        if self._chat_done is not None:
            chat, self._chat_done = self._chat_done, None
            self._send(chat)

    # --- tools ---

    def _tool_span(self, tool_call_id: str | None, tool_name: str | None, ts: int) -> _Span:
        trace_id, parent = self._parent()
        self._tool_count += 1
        key = tool_call_id or f"#{self._tool_count}"
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_name,
            "gen_ai.tool.call.id": tool_call_id,
            "gen_ai.tool.type": "function",
            **self._common(),
        }
        return _Span(
            trace_id=trace_id,
            span_id=span_id_for("tool", *self._scope(), key),
            parent_span_id=parent,
            name=f"execute_tool {tool_name}" if tool_name else "execute_tool",
            kind=SPAN_KIND_INTERNAL,
            start_ns=ts,
            attrs=attrs,
        )

    def _on_tool_start(self, event: dict[str, Any], ts: int) -> None:
        self._flush_chat()
        call_id = event.get("tool_call_id")
        span = self._tool_span(call_id, event.get("tool_name"), ts)
        if self.include_content and event.get("tool_args") is not None:
            span.attrs["gen_ai.tool.call.arguments"] = event["tool_args"]
        self._tools[str(call_id or span.span_id)] = span

    def _on_tool_end(self, event: dict[str, Any], ts: int) -> None:
        call_id = event.get("tool_call_id")
        span = self._tools.pop(str(call_id), None)
        if span is None:
            span = self._tool_span(call_id, event.get("tool_name"), ts)
        span.end_ns = ts
        result = self._tool_results.pop(str(call_id), None)
        if event.get("success") is False:
            span.error_type = "tool_error"
            if self.include_content and result is not None:
                span.status_message = _clip(str(result))[:1000]
        elif self.include_content and result is not None:
            span.attrs["gen_ai.tool.call.result"] = _clip(str(result))
        self._send(span)

    def _on_tool_blocked(self, event: dict[str, Any], ts: int) -> None:
        self._flush_chat()
        span = self._tool_span(event.get("tool_call_id"), event.get("tool_name"), ts)
        span.end_ns = ts
        span.error_type = "blocked"
        span.status_message = "denied by permissions or approval"
        span.attrs["rho_agent.tool.blocked"] = True
        if self.include_content and event.get("tool_args") is not None:
            span.attrs["gen_ai.tool.call.arguments"] = event["tool_args"]
        self._send(span)

    def _close_tools(self, ts: int) -> None:
        for span in self._tools.values():
            span.end_ns = ts
            span.error_type = "interrupted"
            span.status_message = "tool call did not finish"
            self._send(span)
        self._tools.clear()

    # --- child sessions ---

    def _on_child_start(self, event: dict[str, Any], ts: int) -> None:
        child_id = event.get("child_session_id")
        if not child_id:
            return
        tool_name = event.get("tool_name")
        call_id = event.get("tool_call_id")
        span: _Span | None = self._tools.get(str(call_id)) if call_id else None
        if span is None and not call_id:
            # Older traces do not record the call ID: use the latest open call
            # of the same tool.
            for open_span in reversed(list(self._tools.values())):
                if open_span.attrs.get("gen_ai.tool.name") == tool_name:
                    span = open_span
                    break
        if span is not None:
            span.attrs["gen_ai.transfer.mode"] = "return_to_caller"
            span.attrs["gen_ai.transfer.target.name"] = tool_name
            span.attrs["rho_agent.child_session_id"] = child_id
            trace_id, parent_id = span.trace_id, span.span_id
        elif call_id:
            trace_id, _ = self._parent()
            parent_id = span_id_for("tool", *self._scope(), call_id)
        else:
            trace_id, run_parent = self._parent()
            parent_id = run_parent or ""
        self.child_links.setdefault(str(child_id), []).append(
            ChildLink(
                child_session_id=str(child_id),
                tool_name=tool_name,
                trace_id=trace_id,
                parent_span_id=parent_id,
                trace_path=event.get("trace_path"),
            )
        )

    def context_for_child_run(
        self, child_session_id: str, run_index: int
    ) -> tuple[str, str | None]:
        """Trace and parent span for a child's ``run_index``-th run."""
        links = self.child_links.get(child_session_id) or []
        if not links:
            return trace_id_for(child_session_id, run_index), None
        link = links[min(run_index, len(links)) - 1]
        return link.trace_id, link.parent_span_id or None

    # --- run-level events ---

    def _on_compact(self, event: dict[str, Any], ts: int) -> None:
        self._run_event(
            "rho_agent.compact",
            ts,
            {
                "method": event.get("method"),
                "trigger": event.get("trigger"),
                "tokens_before": event.get("tokens_before"),
                "tokens_after": event.get("tokens_after"),
            },
        )

    def _on_approval_requested(self, event: dict[str, Any], ts: int) -> None:
        self._run_event(
            "rho_agent.approval_requested",
            ts,
            {"tool_name": event.get("tool_name"), "request_id": event.get("request_id")},
        )

    def _on_approval_resolved(self, event: dict[str, Any], ts: int) -> None:
        self._run_event(
            "rho_agent.approval_resolved",
            ts,
            {
                "tool_name": event.get("tool_name"),
                "request_id": event.get("request_id"),
                "outcome": event.get("outcome"),
            },
        )

    def _on_completion_gate(self, event: dict[str, Any], ts: int) -> None:
        self._run_event("rho_agent.completion_gate", ts, {"round": event.get("round")})


# --- Offline conversion --------------------------------------------------------


def read_events(trace_path: str | Path) -> list[dict[str, Any]]:
    """Read all events from a trace.jsonl file, skipping torn lines."""
    events: list[dict[str, Any]] = []
    with open(trace_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn final line from a killed process
    return events


def trace_to_spans(
    trace_path: str | Path,
    *,
    session_id: str,
    agent_name: str | None = None,
    provider_name: str = "openai",
    include_content: bool = False,
) -> list[dict[str, Any]]:
    """Convert a session's trace.jsonl (and its children's) into OTLP/JSON spans.

    Child sessions are read from ``children/<id>/trace.jsonl`` next to the
    trace file, or from the path recorded in their ``child_start`` event.
    """
    spans: list[dict[str, Any]] = []
    _convert_session(
        Path(trace_path),
        session_id=session_id,
        root_session_id=session_id,
        context_for_run=None,
        agent_name=agent_name,
        provider_name=provider_name,
        include_content=include_content,
        out=spans,
        seen=set(),
    )
    return spans


def _convert_session(
    trace_path: Path,
    *,
    session_id: str,
    root_session_id: str,
    context_for_run: ContextForRun | None,
    agent_name: str | None,
    provider_name: str,
    include_content: bool,
    out: list[dict[str, Any]],
    seen: set[str],
) -> None:
    seen.add(str(trace_path.resolve()))
    builder = SessionSpanBuilder(
        session_id,
        out.append,
        context_for_run=context_for_run,
        root_session_id=root_session_id,
        agent_name=agent_name,
        provider_name=provider_name,
        include_content=include_content,
    )
    for event in read_events(trace_path):
        builder.feed(event)
    builder.finish()

    for child_id, links in builder.child_links.items():
        child_trace = trace_path.parent / "children" / child_id / "trace.jsonl"
        if not child_trace.exists() and links[0].trace_path:
            child_trace = Path(links[0].trace_path)
        if not child_trace.exists() or str(child_trace.resolve()) in seen:
            continue

        def child_context(n: int, _b: SessionSpanBuilder = builder, _id: str = child_id):
            return _b.context_for_child_run(_id, n)

        _convert_session(
            child_trace,
            session_id=child_id,
            root_session_id=root_session_id,
            context_for_run=child_context,
            agent_name=links[0].tool_name,
            provider_name=provider_name,
            include_content=include_content,
            out=out,
            seen=seen,
        )


def build_request(
    spans: Iterable[dict[str, Any]], *, service_version: str | None = None
) -> dict[str, Any]:
    """Wrap spans in an OTLP ExportTraceServiceRequest (JSON form)."""
    version = service_version or package_version()
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": encode_attributes(
                        {"service.name": SERVICE_NAME, "service.version": version}
                    )
                },
                "scopeSpans": [
                    {"scope": {"name": SCOPE_NAME, "version": version}, "spans": list(spans)}
                ],
            }
        ]
    }


def trace_to_otlp(
    trace_path: str | Path,
    *,
    session_id: str,
    agent_name: str | None = None,
    provider_name: str = "openai",
    include_content: bool = False,
    service_version: str | None = None,
) -> dict[str, Any]:
    """Convert a session's trace.jsonl into an OTLP/JSON ExportTraceServiceRequest."""
    spans = trace_to_spans(
        trace_path,
        session_id=session_id,
        agent_name=agent_name,
        provider_name=provider_name,
        include_content=include_content,
    )
    return build_request(spans, service_version=service_version)


# --- Sending -------------------------------------------------------------------


def package_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("rho-agent")
    except Exception:
        from .. import __version__

        return __version__


_PROVIDER_HOSTS = {
    "openrouter.ai": "openrouter",
    "api.x.ai": "x_ai",
    "groq.com": "groq",
    "deepseek.com": "deepseek",
    "mistral.ai": "mistral",
    "anthropic.com": "anthropic",
}


def provider_name_for(base_url: str | None) -> str:
    """``gen_ai.provider.name`` for an OpenAI-compatible base URL.

    Unknown hosts (vLLM, Ollama, proxies) are reported as ``openai``, the API
    the client speaks.
    """
    host = (urlparse(base_url).hostname or "") if base_url else ""
    for suffix, name in _PROVIDER_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return "openai"


def traces_url(endpoint: str) -> str:
    """Full OTLP/HTTP traces URL for a base endpoint (``/v1/traces`` appended)."""
    url = endpoint.strip().rstrip("/")
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"OTLP endpoint must be an http(s) URL: {endpoint!r}")
    return url if url.endswith("/v1/traces") else url + "/v1/traces"


def parse_headers(raw: str | None) -> dict[str, str]:
    """Parse ``key=value,key2=value2`` (values URL-encoded, as in OTEL_* headers)."""
    headers: dict[str, str] = {}
    for item in (raw or "").split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            headers[key.strip()] = unquote(value.strip())
    return headers


def protobuf_available() -> bool:
    try:
        import google.protobuf  # noqa: F401
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2  # noqa: F401
    except ImportError:
        return False
    return True


def encode_protobuf(request: dict[str, Any]) -> bytes:
    """Encode an OTLP/JSON request as protobuf (needs the ``otel`` extra)."""
    try:
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as e:
        raise RuntimeError(
            "http/protobuf needs the otel extra: pip install 'rho-agent[otel]' "
            "(or use the http/json protocol)"
        ) from e
    # OTLP/JSON writes IDs as hex; the protobuf JSON mapping expects base64.
    converted = copy.deepcopy(request)
    for rs in converted.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if span.get(key):
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    return ParseDict(converted, ExportTraceServiceRequest()).SerializeToString()


def send_request(
    request: dict[str, Any],
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    protocol: str = "http/protobuf",
    timeout: float = 10.0,
) -> None:
    """POST one OTLP request to ``endpoint``. Raises on HTTP or network errors."""
    if protocol == "http/json":
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    elif protocol == "http/protobuf":
        body = encode_protobuf(request)
        content_type = "application/x-protobuf"
    else:
        raise ValueError(f"Unsupported OTLP protocol {protocol!r}; use one of {PROTOCOLS}")
    req = urllib.request.Request(
        traces_url(endpoint),
        data=body,
        method="POST",
        headers={**(headers or {}), "Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def send_spans(
    spans: list[dict[str, Any]],
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    protocol: str = "http/protobuf",
    batch_size: int = 512,
    timeout: float = 10.0,
    service_version: str | None = None,
) -> int:
    """Send spans in batches. Returns the number of spans sent."""
    for i in range(0, len(spans), batch_size):
        request = build_request(spans[i : i + batch_size], service_version=service_version)
        send_request(request, endpoint, headers=headers, protocol=protocol, timeout=timeout)
    return len(spans)
