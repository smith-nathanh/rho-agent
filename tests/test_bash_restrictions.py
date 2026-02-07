from rho_agent.tools.handlers.bash import is_command_allowed


def test_is_command_allowed_does_not_match_dd_inside_heredoc_body() -> None:
    command = """cat <<'PY'\nadded data line\nPY"""
    allowed, reason = is_command_allowed(command)
    assert allowed is True
    assert reason == ""


def test_is_command_allowed_blocks_actual_dd_command() -> None:
    allowed, reason = is_command_allowed("dd if=/dev/zero of=/tmp/out.bin")
    assert allowed is False
    assert "dd" in reason
