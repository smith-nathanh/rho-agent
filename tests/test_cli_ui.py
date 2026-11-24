"""Tests for the redesigned CLI and Monitor UI components."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from prompt_toolkit.document import Document
from rich.console import Console

from rho_agent.cli.completion import COMMANDS, MonitorCompleter, SlashCommandCompleter
from rho_agent.cli.formatting import TokenStatus, _tool_succeeded
from rho_agent.cli.monitor_live import MissionControl, render_trace_event
from rho_agent.cli.theme import THEME
from rho_agent.cli.ui import (
    TurnRenderer,
    render_approval_card,
    render_tool_block,
    render_tool_end,
    render_tool_start,
    render_turn_footer,
    render_user_prompt,
)
from rho_agent.core.approvals import pending_requests
from rho_agent.core.events import AgentEvent
from rho_agent.core.session_store import SessionStore


def _render_to_str(renderable, width: int = 120) -> str:
    c = Console(width=width, force_terminal=False, color_system=None)
    with c.capture() as cap:
        c.print(renderable)
    return cap.get()


def test_edit_tool_renders_inline_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "pricing.py"
    out = _render_to_str(
        render_tool_start(
            "edit",
            {
                "path": str(target),
                "old_string": "if qty > 10:\n    return 0.9",
                "new_string": "if qty >= 10:\n    return 0.9",
            },
            working_dir=str(tmp_path),
        )
    )
    assert "edit" in out
    assert "pricing.py" in out
    assert "+1" in out and "-1" in out
    assert "-if qty > 10:" in out
    assert "+if qty >= 10:" in out


def test_tool_end_marks_failed_bash_as_error() -> None:
    failed_json = json.dumps({"exit_code": 1, "output": "AssertionError"})
    assert _tool_succeeded("bash", {"exit_code": 1}, failed_json) is False
    out = _render_to_str(
        render_tool_end("bash", {"exit_code": 1, "duration_seconds": 0.4}, failed_json)
    )
    assert "×" in out
    assert "exit 1" in out
    assert "AssertionError" in out

    ok_json = json.dumps({"exit_code": 0, "output": "4 passed"})
    assert _tool_succeeded("bash", {"exit_code": 0}, ok_json) is True
    ok_out = _render_to_str(
        render_tool_end("bash", {"exit_code": 0, "duration_seconds": 0.2}, ok_json)
    )
    assert "exit 0" in ok_out
    assert "×" not in ok_out


def test_approval_card_escalates_border_for_dangerous_targets() -> None:
    safe_panel, _ = render_approval_card(
        "request_access",
        {"path": "/tmp/other-proj", "access": "write", "reason": "update shared lib"},
    )
    assert safe_panel.border_style == THEME.border_active

    home_panel, _ = render_approval_card(
        "request_access",
        {"path": str(Path.home()), "access": "write"},
    )
    assert home_panel.border_style == THEME.error

    unsandboxed_panel, _ = render_approval_card(
        "bash",
        {"command": "docker ps", "unsandboxed": True},
    )
    assert unsandboxed_panel.border_style == THEME.error


def test_token_status_toolbar_and_turn_footer() -> None:
    status = TokenStatus(
        input_tokens=40_000,
        output_tokens=1_200,
        model="gpt-6.1-terra",
        access_level="workspace",
        context_window=100_000,
        cost_usd=0.025,
    )
    status.context_size = 25_000
    status.total_cached_tokens = 30_000
    status.total_reasoning_tokens = 400

    toolbar_text = "".join(txt for _, txt in status.render_toolbar())
    assert "workspace" in toolbar_text
    assert "gpt-6.1-terra" in toolbar_text
    assert "25%" in toolbar_text
    assert "75% cached" in toolbar_text
    assert "$0.025" in toolbar_text

    footer = _render_to_str(
        render_turn_footer(
            {
                "context_size": 25_000,
                "total_input_tokens": 40_000,
                "total_output_tokens": 1_200,
                "total_cached_tokens": 30_000,
                "total_reasoning_tokens": 400,
                "total_cost_usd": 0.025,
            },
            model="gpt-6.1-terra",
            elapsed=4.2,
            context_window=100_000,
        )
    )
    assert "25%" in footer
    assert "$0.025" in footer
    assert "4.2s" in footer


def test_slash_completer_has_current_commands_and_no_stale_ones() -> None:
    assert "/access" in COMMANDS
    assert "/status" in COMMANDS
    for stale in ("/approve", "/mode", "/write"):
        assert stale not in COMMANDS

    completer = SlashCommandCompleter()
    matches = [c.text for c in completer.get_completions(Document("/acc"), MagicMock())]
    assert "/access" in matches
    assert "/access read-only" in matches


def test_monitor_completer_and_mission_control_layout(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    sdir = store.base_dir / "aa11bb22"
    sdir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (sdir / "meta.json").write_text(
        json.dumps(
            {
                "id": "aa11bb22",
                "status": "waiting_approval",
                "model": "gpt-6.1-terra",
                "access": "workspace",
                "cwd": str(tmp_path),
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "first_prompt": "Fix the billing tests",
                "cost_usd": 0.12,
                "started_at": now,
                "last_activity": now,
            }
        ),
        encoding="utf-8",
    )
    (sdir / "approvals").mkdir()
    (sdir / "approvals" / "req999.json").write_text(
        json.dumps(
            {
                "id": "req999",
                "session_id": "aa11bb22",
                "tool_name": "bash",
                "tool_args": {"command": "docker ps", "unsandboxed": True},
                "requested_at": now,
            }
        ),
        encoding="utf-8",
    )
    (sdir / "trace.jsonl").write_text(
        json.dumps({"type": "run_start", "prompt": "Fix the billing tests"}) + "\n",
        encoding="utf-8",
    )

    completer = MonitorCompleter(store)
    session_comps = [c.text for c in completer.get_completions(Document("watch aa"), MagicMock())]
    assert session_comps == ["aa11bb22"]

    req_comps = [c.text for c in completer.get_completions(Document("approve req"), MagicMock())]
    assert req_comps == ["req999"]

    mc = MissionControl(store)
    rendered = _render_to_str(mc.build_layout(), width=120)
    assert "aa11bb22" in rendered
    assert "req999" in rendered
    assert "Fix the billing tests" in rendered

    # Approving from MissionControl writes the response file
    mc._answer_selected_or_first(approved=True)
    assert pending_requests(sdir) == []


def test_render_trace_event_formats_tools_without_raw_json() -> None:
    line = json.dumps(
        {
            "type": "tool_start",
            "tool_name": "bash",
            "tool_args": {"command": "pytest -q"},
        }
    )
    out = _render_to_str(render_trace_event(line))
    assert "bash" in out
    assert "pytest -q" in out
    assert '{"command"' not in out


def test_user_prompt_and_compact_tool_blocks_and_no_duplicate_errors(tmp_path: Path) -> None:
    prompt_out = _render_to_str(render_user_prompt("what is this project about?"))
    assert "┃" in prompt_out
    assert "what is this project about?" in prompt_out

    # Compact read omits verbose line dumps
    read_block = _render_to_str(
        render_tool_block(
            "read",
            {"path": str(tmp_path / "README.md")},
            {"start_line": 1, "end_line": 50, "total_lines": 50},
            "1  # big file\n2  lots of noise\n",
            working_dir=str(tmp_path),
        )
    )
    assert "README.md" in read_block
    assert "lines 1–50 of 50" in read_block
    assert "lots of noise" not in read_block

    # Failed tool does not print the identical error line twice
    err_block = _render_to_str(
        render_tool_block(
            "read",
            {"path": str(tmp_path)},
            None,
            "Not a file: /tmp/foo",
            working_dir=str(tmp_path),
        )
    )
    assert err_block.count("Not a file: /tmp/foo") == 1


def test_parallel_tools_commit_atomically_without_interleaving() -> None:
    c = Console(width=100, force_terminal=False, color_system=None)
    renderer = TurnRenderer(console=c, interactive_tty=True, render_markdown=False)
    with c.capture() as cap:
        renderer.on_event(
            AgentEvent(
                type="tool_start", tool_name="read", tool_call_id="c1", tool_args={"path": "a.py"}
            )
        )
        renderer.on_event(
            AgentEvent(
                type="tool_start", tool_name="list", tool_call_id="c2", tool_args={"path": "."}
            )
        )
        renderer.on_event(
            AgentEvent(
                type="tool_end",
                tool_name="read",
                tool_call_id="c1",
                tool_metadata={"start_line": 1, "end_line": 10, "total_lines": 10},
                tool_result="1  x = 1",
            )
        )
        renderer.on_event(
            AgentEvent(
                type="tool_end",
                tool_name="list",
                tool_call_id="c2",
                tool_metadata={"item_count": 5},
                tool_result="a.py\nb.py",
            )
        )
        renderer.stop()
    lines = [ln.strip() for ln in cap.get().splitlines() if ln.strip()]
    assert any("a.py" in ln and "lines 1–10 of 10" in ln for ln in lines)
    assert any("list" in ln and "5 items" in ln for ln in lines)


def test_tool_end_shows_elapsed_when_summary_contains_letter_s() -> None:
    out = _render_to_str(render_tool_end("grep", {"matches": 5}, "a\nb", elapsed=0.3))
    assert "5 matches" in out
    assert "0.3s" in out

    # Does not duplicate when summary already includes a duration
    bash_out = _render_to_str(
        render_tool_end(
            "bash", {"exit_code": 0, "duration_seconds": 0.4}, '{"exit_code": 0}', elapsed=0.5
        )
    )
    assert "0.4s" in bash_out
    assert "0.5s" not in bash_out


def test_malformed_read_offset_does_not_crash() -> None:
    out = _render_to_str(
        render_tool_start("read", {"path": "foo.py", "offset": "bad", "limit": "ten"})
    )
    assert "foo.py" in out


def test_markup_injection_safety_in_tables(tmp_path: Path) -> None:
    from rho_agent.cli import monitor, session_table
    from rho_agent.cli.ui import render_saved_sessions_table

    store = SessionStore(tmp_path / "sessions")
    sdir = store.base_dir / "deadbeef"
    sdir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (sdir / "meta.json").write_text(
        json.dumps(
            {
                "id": "deadbeef",
                "status": "waiting_approval",
                "model": "gpt[v2]/[/bold]",
                "access": "workspace",
                "cwd": str(tmp_path / "[bracket-dir]"),
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "user": "u[/red]",
                "first_prompt": "run pytest tests/test_x.py[param]",
                "cost_usd": 0.01,
                "started_at": now,
                "last_activity": now,
            }
        ),
        encoding="utf-8",
    )
    (sdir / "approvals").mkdir()
    (sdir / "approvals" / "req1.json").write_text(
        json.dumps(
            {
                "id": "req1",
                "session_id": "deadbeef",
                "tool_name": "bash",
                "tool_args": {"command": 'echo "[/bold]" && pytest test.py[param]'},
                "requested_at": now,
            }
        ),
        encoding="utf-8",
    )

    sessions = store.list()
    # None of these should raise MarkupError or strip literal brackets
    saved_out = _render_to_str(render_saved_sessions_table(sessions))
    assert "gpt[v2]/[/bold]" in saved_out

    with session_table.console.capture() as cap:
        session_table.print_sessions(sessions)
    assert "[bracket-dir]" in cap.get()

    mon = monitor.MonitorSession(store.base_dir)
    with monitor.console.capture() as cap:
        mon._cmd_approvals()
    out = cap.get()
    assert "[/bold]" in out
    assert "test.py[param]" in out


def test_read_key_nonblocking_drains_long_escape_sequences() -> None:
    from rho_agent.cli.monitor_live import _read_key_nonblocking

    r_fd, w_fd = os.pipe()
    try:
        # Shift+Up (\x1b[1;2A) followed by 'q': trailing 'A' must NOT leak as approve-all
        os.write(w_fd, b"\x1b[1;2Aq")
        assert _read_key_nonblocking(0.1, fd=r_fd) == "ESC_SEQ"
        assert _read_key_nonblocking(0.1, fd=r_fd) == "q"

        # Standard CSI and SS3 arrow sequences
        os.write(w_fd, b"\x1b[A\x1b[B\x1bOA\x1bOB")
        assert _read_key_nonblocking(0.1, fd=r_fd) == "UP"
        assert _read_key_nonblocking(0.1, fd=r_fd) == "DOWN"
        assert _read_key_nonblocking(0.1, fd=r_fd) == "UP"
        assert _read_key_nonblocking(0.1, fd=r_fd) == "DOWN"
    finally:
        os.close(r_fd)
        os.close(w_fd)


def test_turn_renderer_clears_active_tools_and_non_tty_blocked() -> None:
    # Interactive tool_blocked clears sibling active tools
    c = Console(width=100, force_terminal=False, color_system=None)
    r = TurnRenderer(console=c, interactive_tty=True, render_markdown=True)
    with c.capture() as cap:
        r.on_event(
            AgentEvent(
                type="tool_start",
                tool_name="bash",
                tool_call_id="t1",
                tool_args={"command": "rm -rf /"},
            )
        )
        r.on_event(
            AgentEvent(
                type="tool_start", tool_name="read", tool_call_id="t2", tool_args={"path": "x.py"}
            )
        )
        r.on_event(AgentEvent(type="tool_blocked", tool_name="bash", tool_call_id="t1"))
        assert r._active_tools == {}
        r.on_event(AgentEvent(type="text", content="Understood, stopping."))
        r.on_event(AgentEvent(type="turn_complete", usage={}))
        r.stop()
    out = cap.get()
    assert "rejected by user" in out
    assert "ρ" in out
    assert "Understood, stopping." in out

    # Non-TTY tool_blocked does not print tool header twice
    c2 = Console(width=100, force_terminal=False, color_system=None)
    r2 = TurnRenderer(console=c2, interactive_tty=False, render_markdown=False)
    with c2.capture() as cap2:
        r2.on_event(
            AgentEvent(
                type="tool_start",
                tool_name="bash",
                tool_call_id="t1",
                tool_args={"command": "echo hi"},
            )
        )
        r2.on_event(AgentEvent(type="tool_blocked", tool_name="bash", tool_call_id="t1"))
        r2.stop()
    out2 = cap2.get()
    assert out2.count("echo hi") == 1
    assert "rejected by user" in out2


def test_tail_trace_lines_bounded_seek_and_narrow_mission_control(tmp_path: Path) -> None:
    from rho_agent.cli.monitor_live import _tail_trace_lines

    store = SessionStore(tmp_path / "sessions")
    now = datetime.now(UTC).isoformat()

    # Create 18 sessions to exercise viewport scrolling and narrow layout
    for i in range(18):
        sid = f"aa0000{i:02x}"
        sdir = store.base_dir / sid
        sdir.mkdir(parents=True)
        (sdir / "meta.json").write_text(
            json.dumps(
                {
                    "id": sid,
                    "status": "running" if i == 0 else "completed",
                    "model": "gpt-6.1-terra",
                    "cwd": str(tmp_path),
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "first_prompt": f"Task {i}",
                    "started_at": now,
                    "last_activity": now,
                }
            ),
            encoding="utf-8",
        )

    # Write > 80KB trace on the first session to test bounded EOF seek
    big_dir = store.base_dir / "aa000000"
    with open(big_dir / "trace.jsonl", "w", encoding="utf-8") as f:
        for i in range(1200):
            f.write(
                json.dumps(
                    {"type": "message", "role": "assistant", "content": f"line-{i} " + ("x" * 60)}
                )
                + "\n"
            )

    tailed = _tail_trace_lines(big_dir, limit=5)
    assert len(tailed) == 5
    assert "line-1199" in tailed[-1]

    mc = MissionControl(store)
    mc._move_selection(10)
    c = Console(width=70, height=16, force_terminal=False, color_system=None)
    with c.capture() as cap:
        c.print(mc.build_layout())
    assert "Fleet" in cap.get()


def test_theme_switching_persistence_and_completions(tmp_path: Path, monkeypatch) -> None:
    from rho_agent.cli.theme import (
        PTK_STYLE,
        THEMES,
        load_theme,
        resolve_theme_name,
        set_theme,
    )
    from rho_agent.cli.ui import render_themes_card

    cfg = tmp_path / "theme"
    prev = THEME.name
    try:
        # Every theme keeps a unified brand accent (mark == accent == prompt)
        for preset in THEMES.values():
            assert preset.mark == preset.accent == preset.prompt

        # Default fallback is stone (Stone & Terracotta) with terracotta rho mark
        monkeypatch.delenv("RHO_AGENT_THEME", raising=False)
        default_t = load_theme(config_file=cfg)
        assert default_t.name == "stone"
        assert default_t.mark == "#D97757"
        assert resolve_theme_name("terracotta") == "stone"
        assert resolve_theme_name("slate") == "dusk"
        assert resolve_theme_name("sage") == "teal"

        # Live switch updates THEME proxy, PTK_STYLE, and persists when requested
        chosen = set_theme("teal", persist=True, config_file=cfg)
        assert chosen.name == "teal"
        assert THEME.name == "teal"
        assert THEME.accent == "#52B8B2"
        assert cfg.read_text(encoding="utf-8").strip() == "teal"
        assert load_theme(config_file=cfg).name == "teal"
        assert PTK_STYLE.style_rules  # DynamicStyle resolves without error

        # Env var overrides saved file
        monkeypatch.setenv("RHO_AGENT_THEME", "obsidian")
        assert load_theme(config_file=cfg).name == "obsidian"

        # Invalid theme raises ValueError
        try:
            set_theme("nonexistent")
        except ValueError as exc:
            assert "Unknown theme" in str(exc)
        else:
            raise AssertionError("Expected ValueError for unknown theme")

        # Card renders all palettes
        card_out = _render_to_str(render_themes_card())
        for name in THEMES:
            assert name in card_out

        # Slash and monitor completions expose /theme and theme <name>
        slash_matches = [
            c.text
            for c in SlashCommandCompleter().get_completions(Document("/theme "), MagicMock())
        ]
        assert {f"/theme {name}" for name in THEMES} <= set(slash_matches)

        mon_matches = [
            c.text
            for c in MonitorCompleter(SessionStore(tmp_path / "s")).get_completions(
                Document("theme "), MagicMock()
            )
        ]
        assert set(THEMES) <= set(mon_matches)
    finally:
        set_theme(prev)
