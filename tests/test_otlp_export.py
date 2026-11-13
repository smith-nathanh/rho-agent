"""Tests for the OpenTelemetry (OTLP) export of session traces."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State
from rho_agent.export import otlp_observer
from rho_agent.export.otlp import (
    STATUS_CODE_ERROR,
    build_request,
    decode_attributes,
    parse_headers,
    protobuf_available,
    provider_name_for,
    run_span_id,
    send_spans,
    tool_span_id,
    trace_id_for,
    trace_to_otlp,
    trace_to_spans,
    traces_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "otlp" / "sess1"
CONTENT_KEYS = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
}


def _spans(**kwargs: Any) -> list[dict[str, Any]]:
    return trace_to_spans(FIXTURE / "trace.jsonl", session_id="sess1", **kwargs)


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return decode_attributes(span["attributes"])


def _by_id(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["spanId"]: s for s in spans}


def _tool(spans: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    return next(s for s in spans if _attrs(s).get("gen_ai.tool.call.id") == call_id)


# --- span tree -----------------------------------------------------------------


def test_span_tree_follows_runs_model_calls_tools_and_children() -> None:
    spans = _spans()
    by_id = _by_id(spans)
    runs = [s for s in spans if s["name"] == "invoke_agent"]
    assert len(runs) == 2  # the second run was killed but is still exported

    run1 = next(s for s in runs if _attrs(s)["rho_agent.run.index"] == 1)
    assert run1["spanId"] == run_span_id("sess1", 1)
    assert run1["traceId"] == trace_id_for("sess1", 1)
    assert "parentSpanId" not in run1
    assert run1["kind"] == 1  # INTERNAL

    # Every span of run 1 (child session included) is in run 1's trace.
    trace1 = [s for s in spans if s["traceId"] == run1["traceId"]]
    assert {s["name"] for s in trace1} == {
        "invoke_agent",
        "chat gpt-6.1-terra",
        "execute_tool bash",
        "execute_tool delegate",
        "execute_tool write",
        "invoke_agent delegate",
    }
    own = [s for s in trace1 if s.get("parentSpanId") == run1["spanId"]]
    assert sum(s["name"].startswith("chat") for s in own) == 5  # error, 2 turns, compaction, final
    assert all(s["kind"] == 3 for s in own if s["name"].startswith("chat"))  # CLIENT

    # The child agent hangs off the tool call that spawned it.
    delegate = _tool(spans, "call_2")
    child_run = next(s for s in spans if s["name"] == "invoke_agent delegate")
    assert child_run["parentSpanId"] == delegate["spanId"]
    assert delegate["spanId"] == tool_span_id("sess1", 1, "call_2")
    child_chat = next(s for s in spans if s.get("parentSpanId") == child_run["spanId"])
    assert child_chat["name"] == "chat gpt-6.1-terra"

    # Run 2 is its own trace.
    run2 = next(s for s in runs if _attrs(s)["rho_agent.run.index"] == 2)
    assert run2["traceId"] == trace_id_for("sess1", 2) != run1["traceId"]
    for s in spans:
        if s.get("parentSpanId"):
            assert s["parentSpanId"] in by_id


def test_genai_attributes() -> None:
    spans = _spans()
    run1 = next(s for s in spans if s["spanId"] == run_span_id("sess1", 1))
    attrs = _attrs(run1)
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.conversation.id"] == "sess1"
    assert attrs["session.id"] == "sess1"
    assert attrs["gen_ai.request.model"] == "gpt-6.1-terra"
    assert attrs["rho_agent.run.status"] == "completed"
    # Run totals include compaction and the child's usage but are kept out of
    # gen_ai.usage.* so trace-level sums do not double count.
    assert attrs["rho_agent.usage.input_tokens"] == 200 + 260 + 100 + 5000 + 300
    assert attrs["rho_agent.usage.cost_usd"] == pytest.approx(0.017)
    assert not any(k.startswith("gen_ai.usage.") for k in attrs)
    assert "status" not in run1

    first_ok = next(
        s
        for s in spans
        if s["name"].startswith("chat") and _attrs(s).get("gen_ai.usage.input_tokens") == 200
    )
    chat = _attrs(first_ok)
    assert chat["gen_ai.operation.name"] == "chat"
    assert chat["gen_ai.provider.name"] == "openai"
    assert chat["gen_ai.request.model"] == "gpt-6.1-terra"
    assert chat["gen_ai.usage.output_tokens"] == 20
    assert chat["gen_ai.usage.cache_read.input_tokens"] == 50
    assert chat["gen_ai.usage.reasoning.output_tokens"] == 5
    assert chat["rho_agent.cost_usd"] == pytest.approx(0.002)
    assert chat["gen_ai.usage.cost"] == pytest.approx(0.002)

    tool = _attrs(_tool(spans, "call_1"))
    assert tool["gen_ai.operation.name"] == "execute_tool"
    assert tool["gen_ai.tool.name"] == "bash"
    assert tool["gen_ai.tool.type"] == "function"

    delegate = _attrs(_tool(spans, "call_2"))
    assert delegate["gen_ai.transfer.mode"] == "return_to_caller"
    assert delegate["gen_ai.transfer.target.name"] == "delegate"
    assert delegate["rho_agent.child_session_id"] == "c1"

    child_run = _attrs(next(s for s in spans if s["name"] == "invoke_agent delegate"))
    assert child_run["gen_ai.agent.name"] == "delegate"
    assert child_run["gen_ai.conversation.id"] == "c1"
    assert child_run["session.id"] == "sess1"


def test_errors_blocks_and_interruptions() -> None:
    spans = _spans()
    chats = [s for s in spans if s["name"].startswith("chat")]
    failed = [s for s in chats if s.get("status", {}).get("code") == STATUS_CODE_ERROR]
    assert [_attrs(s)["error.type"] for s in failed] == ["transient"]
    assert failed[0]["status"]["message"] == "transient"  # error text only with content

    bash = _tool(spans, "call_1")
    assert bash["status"]["code"] == STATUS_CODE_ERROR
    assert _attrs(bash)["error.type"] == "tool_error"

    blocked = _tool(spans, "call_3")
    assert _attrs(blocked)["error.type"] == "blocked"
    assert _attrs(blocked)["rho_agent.tool.blocked"] is True
    assert blocked["startTimeUnixNano"] == blocked["endTimeUnixNano"]

    # The killed run and its unfinished tool call are closed at the last event.
    run2 = next(s for s in spans if s["spanId"] == run_span_id("sess1", 2))
    assert _attrs(run2)["rho_agent.run.status"] == "unfinished"
    assert run2["status"]["code"] == STATUS_CODE_ERROR
    lint = _tool(spans, "call_9")
    assert _attrs(lint)["error.type"] == "interrupted"
    assert lint["endTimeUnixNano"] == run2["endTimeUnixNano"]


def test_compaction_and_run_events() -> None:
    spans = _spans()
    chats = [_attrs(s) for s in spans if s["name"].startswith("chat")]
    compaction = [a for a in chats if a.get("rho_agent.purpose") == "compaction"]
    assert len(compaction) == 1
    assert compaction[0]["gen_ai.usage.input_tokens"] == 5000
    # Calls after the history was replaced are flagged as compacted.
    final = next(a for a in chats if a.get("gen_ai.usage.input_tokens") == 300)
    assert final["gen_ai.conversation.compacted"] is True

    run1 = next(s for s in spans if s["spanId"] == run_span_id("sess1", 1))
    names = [e["name"] for e in run1["events"]]
    assert names == [
        "rho_agent.approval_requested",
        "rho_agent.approval_resolved",
        "rho_agent.compact",
    ]
    resolved = decode_attributes(run1["events"][1]["attributes"])
    assert resolved["outcome"] == "denied"


def test_timestamps_come_from_trace_events() -> None:
    spans = _spans()
    bash = _tool(spans, "call_1")
    # 2026-02-21T10:00:02.010Z .. 10:00:03.001Z
    assert int(bash["endTimeUnixNano"]) - int(bash["startTimeUnixNano"]) == 991_000_000
    assert bash["startTimeUnixNano"] == "1771668002010000000"
    for s in spans:
        assert int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"])


def test_ids_are_deterministic_and_unique() -> None:
    first, second = _spans(), _spans()
    assert first == second
    ids = [s["spanId"] for s in first]
    assert len(ids) == len(set(ids))
    assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in first)


# --- content -------------------------------------------------------------------


def test_content_is_left_out_by_default() -> None:
    for span in _spans():
        assert not CONTENT_KEYS & set(_attrs(span))


def test_include_content_records_messages_and_tool_io() -> None:
    spans = _spans(include_content=True)
    run1 = _attrs(next(s for s in spans if s["spanId"] == run_span_id("sess1", 1)))
    assert json.loads(run1["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "Fix the failing test"}]}
    ]
    assert "parser drops" in json.loads(run1["gen_ai.output.messages"])[0]["parts"][0]["content"]

    second_turn = next(
        _attrs(s)
        for s in spans
        if s["name"].startswith("chat") and _attrs(s).get("gen_ai.usage.input_tokens") == 260
    )
    history = json.loads(second_turn["gen_ai.input.messages"])
    assert [m["role"] for m in history] == ["user", "assistant", "tool"]
    assert history[1]["parts"][0] == {
        "type": "tool_call",
        "id": "call_1",
        "name": "bash",
        "arguments": {"command": "pytest"},
    }
    assert history[2]["parts"][0]["type"] == "tool_call_response"
    output = json.loads(second_turn["gen_ai.output.messages"])
    assert output[0]["finish_reason"] == "tool_call"
    assert [p["type"] for p in output[0]["parts"]] == ["text", "tool_call", "tool_call"]

    # After compaction the input is the replaced history.
    final = next(
        _attrs(s)
        for s in spans
        if s["name"].startswith("chat") and _attrs(s).get("gen_ai.usage.input_tokens") == 300
    )
    assert len(json.loads(final["gen_ai.input.messages"])) == 2

    delegate = _attrs(_tool(spans, "call_2"))
    assert json.loads(delegate["gen_ai.tool.call.arguments"]) == {
        "instruction": "Find why test_parse fails"
    }
    assert delegate["gen_ai.tool.call.result"] == "The parser drops the last token."
    # A failed call's output goes in the status message, not the result.
    bash = _tool(spans, "call_1")
    assert "gen_ai.tool.call.result" not in _attrs(bash)
    assert bash["status"]["message"] == "1 failed: test_parse"


# --- older traces --------------------------------------------------------------


def _write(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_unknown_cost_is_left_out_not_reported_as_zero(tmp_path: Path) -> None:
    # rho records cost_usd 0 for a model it has no price for; a backend with its
    # own price table must not be told the call was free.
    ts = "2026-02-21T10:00:0{}Z".format
    _write(
        tmp_path / "trace.jsonl",
        [
            {"type": "run_start", "prompt": "p", "ts": ts(0)},
            {"type": "llm_start", "model": "m", "ts": ts(1)},
            {"type": "llm_end", "model": "m", "input_tokens": 5, "cost_usd": 0.0, "ts": ts(2)},
            {"type": "run_end", "status": "completed", "ts": ts(3)},
        ],
    )
    spans = trace_to_spans(tmp_path / "trace.jsonl", session_id="free")
    for span in spans:
        attrs = _attrs(span)
        assert not {"gen_ai.usage.cost", "rho_agent.cost_usd", "rho_agent.usage.cost_usd"} & set(
            attrs
        )
    chat = next(s for s in spans if s["name"] == "chat m")
    assert _attrs(chat)["gen_ai.usage.input_tokens"] == 5


def test_older_traces_without_call_ids_or_compaction_start(tmp_path: Path) -> None:
    ts = "2026-02-21T10:00:0{}Z".format
    _write(
        tmp_path / "trace.jsonl",
        [
            {"type": "run_start", "prompt": "p", "ts": ts(0)},
            {"type": "tool_start", "tool_call_id": "a", "tool_name": "agent_x", "ts": ts(1)},
            {"type": "child_start", "tool_name": "agent_x", "child_session_id": "k", "ts": ts(1)},
            {"type": "tool_end", "tool_call_id": "a", "tool_name": "agent_x", "ts": ts(4)},
            {
                "type": "llm_end",
                "model": "m",
                "purpose": "compaction",
                "input_tokens": 9,
                "ts": ts(5),
            },
            {"type": "run_end", "status": "completed", "ts": ts(6)},
        ],
    )
    _write(
        tmp_path / "children" / "k" / "trace.jsonl",
        [
            {"type": "run_start", "prompt": "q", "ts": ts(2)},
            {"type": "run_end", "status": "completed", "ts": ts(3)},
        ],
    )
    spans = trace_to_spans(tmp_path / "trace.jsonl", session_id="old")
    tool = next(s for s in spans if s["name"] == "execute_tool agent_x")
    child = next(s for s in spans if s["name"] == "invoke_agent agent_x")
    assert child["parentSpanId"] == tool["spanId"]
    compaction = next(s for s in spans if s["name"] == "chat m")
    # No llm_start: the span starts at the previous event.
    assert int(compaction["endTimeUnixNano"]) - int(compaction["startTimeUnixNano"]) == 10**9


def test_request_envelope() -> None:
    request = trace_to_otlp(FIXTURE / "trace.jsonl", session_id="sess1", service_version="9.9")
    rs = request["resourceSpans"][0]
    assert decode_attributes(rs["resource"]["attributes"]) == {
        "service.name": "rho-agent",
        "service.version": "9.9",
    }
    scope = rs["scopeSpans"][0]
    assert scope["scope"] == {"name": "rho_agent", "version": "9.9"}
    assert len(scope["spans"]) == len(_spans())


# --- helpers -------------------------------------------------------------------


def test_endpoint_headers_and_provider_helpers() -> None:
    assert traces_url("http://localhost:4318") == "http://localhost:4318/v1/traces"
    assert traces_url("https://h/otel/") == "https://h/otel/v1/traces"
    assert traces_url("http://c:4318/v1/traces") == "http://c:4318/v1/traces"
    with pytest.raises(ValueError):
        traces_url("localhost:4318")
    assert parse_headers("x-api-key=abc, Authorization=Basic%20Zm9v,bad") == {
        "x-api-key": "abc",
        "Authorization": "Basic Zm9v",
    }
    assert parse_headers(None) == {}
    assert provider_name_for(None) == "openai"
    assert provider_name_for("https://openrouter.ai/api/v1") == "openrouter"
    assert provider_name_for("https://api.x.ai/v1") == "x_ai"
    assert provider_name_for("http://localhost:8000/v1") == "openai"


class _Collector:
    """A local OTLP/HTTP receiver that records requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    c = _Collector()
    yield c
    c.close()


def test_send_spans_json_in_batches(collector: _Collector) -> None:
    spans = _spans()
    sent = send_spans(
        spans, collector.url, headers={"x-api-key": "k"}, protocol="http/json", batch_size=5
    )
    assert sent == len(spans)
    assert len(collector.requests) == -(-len(spans) // 5)
    path, headers, body = collector.requests[0]
    assert path == "/v1/traces"
    assert headers["content-type"] == "application/json"
    assert headers["x-api-key"] == "k"
    assert len(json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 5


@pytest.mark.skipif(not protobuf_available(), reason="needs the otel extra")
def test_send_spans_protobuf(collector: _Collector) -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    spans = _spans()
    send_spans(spans, collector.url, protocol="http/protobuf")
    _, headers, body = collector.requests[0]
    assert headers["content-type"] == "application/x-protobuf"
    decoded = ExportTraceServiceRequest.FromString(body)
    got = decoded.resource_spans[0].scope_spans[0].spans
    assert [s.span_id.hex() for s in got] == [s["spanId"] for s in spans]
    assert [s.trace_id.hex() for s in got] == [s["traceId"] for s in spans]
    assert got[0].start_time_unix_nano == int(spans[0]["startTimeUnixNano"])


# --- CLI -----------------------------------------------------------------------


def test_cli_exports_otlp_json() -> None:
    from rho_agent.cli import app

    result = CliRunner().invoke(
        app, ["export", "sess1", "-d", str(FIXTURE.parent), "--format", "otlp"]
    )
    assert result.exit_code == 0, result.output
    request = json.loads(result.output)
    assert request == build_request(_spans(), service_version=_version(request))


def test_cli_rejects_otlp_options_for_atif() -> None:
    from rho_agent.cli import app

    result = CliRunner().invoke(
        app, ["export", "sess1", "-d", str(FIXTURE.parent), "--endpoint", "http://x"]
    )
    assert result.exit_code == 1


def test_cli_sends_to_endpoint(collector: _Collector, monkeypatch: pytest.MonkeyPatch) -> None:
    from rho_agent.cli import app

    monkeypatch.setenv("RHO_AGENT_OTLP_HEADERS", "x-api-key=secret")
    result = CliRunner().invoke(
        app,
        [
            "export",
            "sess1",
            "-d",
            str(FIXTURE.parent),
            "-f",
            "otlp",
            "--endpoint",
            collector.url,
            "--protocol",
            "http/json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"Sent {len(_spans())} spans" in result.output
    assert collector.requests[0][1]["x-api-key"] == "secret"


def _version(request: dict[str, Any]) -> str:
    return request["resourceSpans"][0]["scopeSpans"][0]["scope"]["version"]


# --- live observer ---------------------------------------------------------------


class _Client:
    """Scripted parent turns; a call whose last message is CHILD:... gets a reply."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = list(turns)

    async def stream(self, prompt: Any):
        last = prompt.messages[-1]
        if last.role == "user" and str(last.content).startswith("CHILD:"):
            await asyncio.sleep(0.01)
            yield StreamEvent(type="text", content=f"answer to {last.content}")
            yield StreamEvent(type="done", usage={"input_tokens": 7, "output_tokens": 3})
            return
        for e in self._turns.pop(0):
            yield e


def _call(call_id: str, name: str, **args: Any) -> StreamEvent:
    return StreamEvent(type="tool_call", tool_call=ToolCall(id=call_id, name=name, arguments=args))


def _done() -> StreamEvent:
    return StreamEvent(
        type="done",
        usage={"input_tokens": 10, "output_tokens": 2, "cached_tokens": 4, "cost_usd": 0.1},
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch):
    """Route the live exporter to a list instead of the network."""
    spans: list[dict[str, Any]] = []
    monkeypatch.setenv("RHO_AGENT_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setenv("RHO_AGENT_OTLP_PROTOCOL", "http/json")
    monkeypatch.setenv("RHO_AGENT_OTLP_CONTENT", "1")
    monkeypatch.setattr(
        otlp_observer, "send_spans", lambda batch, *a, **k: spans.extend(batch) or len(batch)
    )
    monkeypatch.setattr(otlp_observer, "_exporters", {})
    yield spans
    for exporter in otlp_observer._exporters.values():
        exporter.flush()


async def test_live_observer_sends_the_same_spans_as_offline_export(
    tmp_path: Path, captured: list[dict[str, Any]]
) -> None:
    client = _Client(
        [
            [
                _call("c1", "delegate", instruction="CHILD: one"),
                _call("c2", "delegate", instruction="CHILD: two"),
                _done(),
            ],
            [StreamEvent(type="text", content="done"), _done()],
            [StreamEvent(type="text", content="again"), _done()],
        ]
    )
    session_dir = tmp_path / "live1"
    session_dir.mkdir()
    agent = Agent(AgentConfig(system_prompt="s", profile="readonly", model="m"))
    session = Session(
        agent,
        session_id="live1",
        client=client,
        state=State(trace_path=session_dir / "trace.jsonl"),
        session_dir=session_dir,
    )
    assert (await session.run("go")).status == "completed"
    assert (await session.run("more")).status == "completed"
    for exporter in otlp_observer._exporters.values():
        exporter.flush()

    offline = trace_to_spans(session_dir / "trace.jsonl", session_id="live1", include_content=True)
    key = lambda s: s["spanId"]
    assert sorted(captured, key=key) == sorted(offline, key=key)
    assert sum(s["name"] == "invoke_agent delegate" for s in captured) == 2
    assert len({s["traceId"] for s in captured}) == 2  # one trace per run


async def test_observer_attaches_once_and_only_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(Agent(AgentConfig(profile="readonly", model="m")), client=_Client([]))
    monkeypatch.delenv("RHO_AGENT_OTLP_ENDPOINT", raising=False)
    assert otlp_observer.maybe_attach_otlp_observer(session) is None

    monkeypatch.setenv("RHO_AGENT_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("RHO_AGENT_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr(otlp_observer, "_warned_config", True)
    assert otlp_observer.maybe_attach_otlp_observer(session) is None

    monkeypatch.setenv("RHO_AGENT_OTLP_PROTOCOL", "http/json")
    monkeypatch.setattr(otlp_observer, "_exporters", {})
    first = otlp_observer.maybe_attach_otlp_observer(session)
    assert first is not None
    assert otlp_observer.maybe_attach_otlp_observer(session) is first
    assert first.builder.include_content is False


def test_exporter_drops_spans_when_the_collector_is_down() -> None:
    exporter = otlp_observer.OtlpExporter("http://127.0.0.1:9", protocol="http/json", timeout=1)
    exporter._warned = True  # keep test output quiet
    exporter.submit({"traceId": "0" * 31 + "1", "spanId": "0" * 15 + "1", "name": "x"})
    exporter.flush(timeout=5)
    assert exporter.dropped == 1 and exporter.sent == 0
