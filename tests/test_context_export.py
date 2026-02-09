from __future__ import annotations

from rho_agent.context_export import serialize_session, write_context_file


def test_serialize_session_empty_messages() -> None:
    text = serialize_session([])
    assert "=== TURN 1 ===" in text
    assert "[empty]" in text


def test_serialize_session_user_assistant_round() -> None:
    messages = [
        {"role": "user", "content": "find root cause"},
        {"role": "assistant", "content": "Checking logs now"},
    ]

    text = serialize_session(messages)
    assert "=== TURN 1 ===" in text
    assert "[user] find root cause" in text
    assert "[assistant] Checking logs now" in text


def test_serialize_session_includes_tool_calls_and_results() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "ls -la /tmp"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file_a\nfile_b"},
    ]

    text = serialize_session(messages)
    assert "[tool:bash] ls -la /tmp" in text
    assert "[tool_result] file_a\nfile_b" in text


def test_serialize_session_truncates_long_tool_results() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 40},
    ]

    text = serialize_session(messages, max_tool_result_chars=10)
    assert "[tool_result] xxxxxxxxxx...[truncated]" in text


def test_write_context_file_writes_content(tmp_path) -> None:
    path = tmp_path / "ctx.context"
    messages = [{"role": "user", "content": "hello"}]

    write_context_file(path, messages)

    assert path.exists()
    assert "[user] hello" in path.read_text(encoding="utf-8")
