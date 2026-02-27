"""Tests for ATIF trace export."""

from __future__ import annotations

import json
from pathlib import Path

from rho_agent.export.atif import trace_to_atif


def _write_trace(tmp_path: Path, events: list[dict]) -> Path:
    trace = tmp_path / "trace.jsonl"
    with open(trace, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return trace


SAMPLE_EVENTS = [
    {"type": "run_start", "prompt": "What is 2+2?", "ts": "2026-01-01T00:00:00Z"},
    {"type": "message", "role": "user", "content": "What is 2+2?", "ts": "2026-01-01T00:00:01Z"},
    {"type": "llm_start", "model": "claude-sonnet-4-20250514", "context_size": 100},
    {
        "type": "llm_end",
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 50,
        "output_tokens": 30,
        "cache_read_tokens": 10,
        "cost_usd": 0.001,
    },
    {
        "type": "message",
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "calculator",
                    "arguments": '{"expression": "2+2"}',
                },
            }
        ],
        "ts": "2026-01-01T00:00:02Z",
    },
    {"type": "tool_start", "tool_call_id": "call_abc123", "tool_name": "calculator"},
    {"type": "tool_end", "tool_call_id": "call_abc123", "tool_name": "calculator", "success": True},
    {
        "type": "message",
        "role": "tool",
        "tool_call_id": "call_abc123",
        "content": "4",
    },
    {
        "type": "llm_end",
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 80,
        "output_tokens": 20,
        "cache_read_tokens": 40,
        "cost_usd": 0.0005,
    },
    {
        "type": "message",
        "role": "assistant",
        "content": "The answer is 4.",
        "ts": "2026-01-01T00:00:03Z",
    },
    {"type": "run_end", "status": "completed"},
    {
        "type": "usage",
        "input_tokens": 130,
        "output_tokens": 50,
        "cached_tokens": 50,
        "cost_usd": 0.0015,
        "status": "completed",
        "run_count": 1,
    },
]


def test_basic_conversion(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(
        trace,
        session_id="test-001",
        agent_name="rho-agent",
        agent_version="0.1.0",
        model_name="claude-sonnet-4-20250514",
    )

    assert result["schema_version"] == "ATIF-v1.6"
    assert result["session_id"] == "test-001"
    assert result["agent"]["name"] == "rho-agent"
    assert result["agent"]["model_name"] == "claude-sonnet-4-20250514"


def test_step_ids_sequential(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(trace, session_id="test-002")

    steps = result["steps"]
    for i, step in enumerate(steps):
        assert step["step_id"] == i + 1, f"step {i} has step_id {step['step_id']}"


def test_step_sources(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(trace, session_id="test-003")

    sources = [s["source"] for s in result["steps"]]
    assert sources == ["user", "agent", "agent"]


def test_tool_calls_and_observations(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(trace, session_id="test-004")

    # The first agent step has the tool call
    agent_step = result["steps"][1]
    assert agent_step["source"] == "agent"
    assert len(agent_step["tool_calls"]) == 1

    tc = agent_step["tool_calls"][0]
    assert tc["tool_call_id"] == "call_abc123"
    assert tc["function_name"] == "calculator"
    assert tc["arguments"] == {"expression": "2+2"}

    # Observation links back to the tool call
    obs = agent_step["observation"]
    assert len(obs["results"]) == 1
    assert obs["results"][0]["source_call_id"] == "call_abc123"
    assert obs["results"][0]["content"] == "4"


def test_metrics_populated(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(trace, session_id="test-005")

    # First agent step should have metrics from the llm_end before it
    agent_step = result["steps"][1]
    assert "metrics" in agent_step
    m = agent_step["metrics"]
    assert m["prompt_tokens"] == 50
    assert m["completion_tokens"] == 30
    assert m["cached_tokens"] == 10
    assert m["cost_usd"] == 0.001

    # Second agent step should have metrics from the llm_end between tool
    # results and the assistant message
    second_step = result["steps"][2]
    assert "metrics" in second_step
    m2 = second_step["metrics"]
    assert m2["prompt_tokens"] == 80
    assert m2["completion_tokens"] == 20


def test_final_metrics(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path, SAMPLE_EVENTS)
    result = trace_to_atif(trace, session_id="test-006")

    fm = result["final_metrics"]
    assert fm["total_prompt_tokens"] == 130
    assert fm["total_completion_tokens"] == 50
    assert fm["total_cached_tokens"] == 50
    assert fm["total_cost_usd"] == 0.0015
    assert fm["total_steps"] == 3


def test_system_message(tmp_path: Path) -> None:
    events = [
        {"type": "message", "role": "system", "content": "You are helpful."},
        {"type": "message", "role": "user", "content": "Hi"},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-007")

    assert result["steps"][0]["source"] == "system"
    assert result["steps"][0]["message"] == "You are helpful."
    assert result["steps"][1]["source"] == "user"


def test_assistant_text_only(tmp_path: Path) -> None:
    """Assistant message with no tool calls produces a step with just message."""
    events = [
        {"type": "message", "role": "user", "content": "Hello"},
        {"type": "message", "role": "assistant", "content": "Hi there!"},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-008")

    agent_step = result["steps"][1]
    assert agent_step["source"] == "agent"
    assert agent_step["message"] == "Hi there!"
    assert "tool_calls" not in agent_step
    assert "observation" not in agent_step


def test_no_model_name_omits_field(tmp_path: Path) -> None:
    events = [{"type": "message", "role": "user", "content": "Hi"}]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-009")
    assert "model_name" not in result["agent"]


def test_empty_trace(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("")
    result = trace_to_atif(trace, session_id="test-010")
    assert result["steps"] == []
    assert "final_metrics" not in result


def test_multiple_tool_calls_single_step(tmp_path: Path) -> None:
    """Assistant with multiple parallel tool calls → single agent step."""
    events = [
        {"type": "message", "role": "user", "content": "Search two things"},
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "foo"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "bar"}'},
                },
            ],
        },
        {"type": "message", "role": "tool", "tool_call_id": "call_1", "content": "result foo"},
        {"type": "message", "role": "tool", "tool_call_id": "call_2", "content": "result bar"},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-011")

    agent_step = result["steps"][1]
    assert len(agent_step["tool_calls"]) == 2
    assert len(agent_step["observation"]["results"]) == 2
    assert agent_step["observation"]["results"][0]["source_call_id"] == "call_1"
    assert agent_step["observation"]["results"][1]["source_call_id"] == "call_2"


def test_assistant_with_content_and_tool_calls(tmp_path: Path) -> None:
    """Assistant message with both text content and tool calls."""
    events = [
        {"type": "message", "role": "user", "content": "Calculate this"},
        {
            "type": "message",
            "role": "assistant",
            "content": "Let me calculate that for you.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"x": 1}'},
                },
            ],
        },
        {"type": "message", "role": "tool", "tool_call_id": "call_1", "content": "1"},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-012")

    step = result["steps"][1]
    assert step["message"] == "Let me calculate that for you."
    assert len(step["tool_calls"]) == 1
    assert step["observation"]["results"][0]["content"] == "1"


def test_dict_arguments(tmp_path: Path) -> None:
    """Tool call arguments already provided as a dict (not JSON string)."""
    events = [
        {"type": "message", "role": "user", "content": "Go"},
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": {"q": "test"}},
                },
            ],
        },
        {"type": "message", "role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-013")

    assert result["steps"][1]["tool_calls"][0]["arguments"] == {"q": "test"}


def test_malformed_arguments_fallback(tmp_path: Path) -> None:
    """Malformed JSON in tool call arguments falls back gracefully."""
    events = [
        {"type": "message", "role": "user", "content": "Go"},
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "not valid json{"},
                },
            ],
        },
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-014")

    assert result["steps"][1]["tool_calls"][0]["arguments"] == {"_raw": "not valid json{"}


def test_tool_calls_without_results(tmp_path: Path) -> None:
    """Assistant with tool calls but no tool results (e.g., blocked)."""
    events = [
        {"type": "message", "role": "user", "content": "Do something"},
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd": "rm -rf /"}'},
                },
            ],
        },
        {"type": "tool_blocked", "tool_call_id": "call_1", "tool_name": "bash"},
        {"type": "message", "role": "assistant", "content": "That was blocked."},
    ]
    trace = _write_trace(tmp_path, events)
    result = trace_to_atif(trace, session_id="test-015")

    # First agent step has tool_calls but no observation
    step1 = result["steps"][1]
    assert len(step1["tool_calls"]) == 1
    assert "observation" not in step1

    # Second agent step is the follow-up text
    step2 = result["steps"][2]
    assert step2["message"] == "That was blocked."
