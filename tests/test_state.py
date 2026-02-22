"""Tests for State serialization, message management, and compaction."""

from __future__ import annotations

from rho_agent.core.state import State


def _make_state(**kwargs) -> State:
    return State(**kwargs)


def test_jsonl_round_trip_messages():
    state = _make_state()
    state.add_user_message("hello")
    state.add_assistant_message("hi there")
    state.status = "completed"

    restored = State.from_jsonl(state.to_jsonl())
    assert len(restored.messages) == 2
    assert restored.messages[0]["role"] == "user"
    assert restored.messages[0]["content"] == "hello"
    assert restored.messages[1]["role"] == "assistant"
    assert restored.messages[1]["content"] == "hi there"


def test_jsonl_round_trip_usage():
    state = _make_state()
    state.update_usage(input_tokens=100, output_tokens=50, cost_usd=0.01)
    state.status = "completed"
    state.run_count = 3

    restored = State.from_jsonl(state.to_jsonl())
    assert restored.usage["input_tokens"] == 100
    assert restored.usage["output_tokens"] == 50
    assert restored.usage["cost_usd"] == 0.01
    assert restored.status == "completed"
    assert restored.run_count == 3


def test_jsonl_round_trip_tool_calls():
    state = _make_state()
    tool_calls = [
        {"id": "tc_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
    ]
    state.add_assistant_tool_calls(tool_calls)
    state.add_tool_result("tc_1", "file1.txt\nfile2.txt")
    state.status = "completed"

    restored = State.from_jsonl(state.to_jsonl())
    assert restored.messages[0]["tool_calls"] == tool_calls
    assert restored.messages[1]["role"] == "tool"
    assert restored.messages[1]["tool_call_id"] == "tc_1"


def test_replace_with_summary_clears_history():
    state = _make_state()
    state.add_user_message("msg1")
    state.add_assistant_message("resp1")
    state.add_user_message("msg2")

    state.replace_with_summary("Summary of conversation")
    assert len(state.messages) == 1
    assert state.messages[0]["content"] == "Summary of conversation"


def test_replace_with_summary_preserves_recent():
    state = _make_state()
    state.add_user_message("msg1")
    state.add_assistant_message("resp1")
    state.add_user_message("msg2")

    state.replace_with_summary("Summary", recent_user_messages=["msg2"])
    assert len(state.messages) == 2
    assert state.messages[0]["content"] == "msg2"
    assert state.messages[1]["content"] == "Summary"


def test_estimate_tokens_returns_positive_int():
    state = _make_state()
    state.add_user_message("Hello, how are you doing today?")
    result = state.estimate_tokens()
    assert isinstance(result, int)
    assert result > 0


def test_estimate_tokens_scales_with_content():
    small = _make_state()
    small.add_user_message("hi")

    large = _make_state()
    large.add_user_message("x" * 1000)

    assert large.estimate_tokens() > small.estimate_tokens()


def test_update_usage_accumulates():
    state = _make_state()
    state.update_usage(input_tokens=100, output_tokens=50, cost_usd=0.01)
    state.update_usage(input_tokens=200, output_tokens=100, cost_usd=0.02)
    assert state.usage["input_tokens"] == 300
    assert state.usage["output_tokens"] == 150
    assert state.usage["cost_usd"] == pytest.approx(0.03)


# Need pytest for approx
import pytest
