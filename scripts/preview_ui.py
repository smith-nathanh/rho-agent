#!/usr/bin/env python3
"""Zero-API-key visual preview for the redesigned rho-agent CLI and Monitor UI.

Usage:
    python scripts/preview_ui.py            # Run both CLI and Monitor previews
    python scripts/preview_ui.py cli        # Preview main CLI visuals
    python scripts/preview_ui.py monitor    # Preview monitor shell & dashboard views
    python scripts/preview_ui.py live       # Launch interactive Mission Control on mock fleet
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

# Ensure the worktree package is imported when invoked directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rich.rule import Rule
from rich.text import Text

from rho_agent.cli.formatting import TokenStatus, _get_version, _markup
from rho_agent.cli.monitor import MonitorSession
from rho_agent.cli.monitor_live import MissionControl
from rho_agent.cli.state import console
from rho_agent.cli.theme import THEME
from rho_agent.cli.ui import (
    make_markdown,
    render_access_card,
    render_agent_divider,
    render_approval_card,
    render_help_card,
    render_session_status_card,
    render_single_header,
    render_tool_block,
    render_turn_footer,
    render_user_prompt,
    render_welcome_banner,
)
from rho_agent.core.session_store import SessionStore


def _mock_session() -> SimpleNamespace:
    sandbox = SimpleNamespace(
        backend="seatbelt",
        with_grants=lambda grants: SimpleNamespace(
            policy=SimpleNamespace(
                describe=lambda: (
                    "Writable: ~/proj/shop, /tmp, package caches\n"
                    "Protected: .git/hooks, .git/config, ~/.ssh, ~/.aws, *_API_KEY"
                )
            )
        ),
    )
    access = SimpleNamespace(
        level=SimpleNamespace(value="workspace"),
        sandboxed=True,
        network=True,
        add_dirs=(),
    )
    config = SimpleNamespace(
        model="gpt-6.1-terra",
        reasoning_effort="medium",
        backend="local",
        access=access,
    )
    agent = SimpleNamespace(
        config=config,
        registry=SimpleNamespace(sandbox=sandbox),
    )
    return SimpleNamespace(
        id="a3f8c1d2",
        agent=agent,
        access_grants=[SimpleNamespace(write=True, path=str(Path.home() / "proj" / "shared-lib"))],
        state=SimpleNamespace(
            messages=[{"role": "user"}, {"role": "assistant"}] * 4,
            usage={
                "input_tokens": 42_180,
                "output_tokens": 1_840,
                "cached_tokens": 34_200,
                "reasoning_tokens": 320,
                "cost_usd": 0.0184,
            },
        ),
    )


def preview_cli() -> None:
    """Render the full main CLI visual experience."""
    console.print(Rule("[bold]1 · Main CLI (rho-agent)[/bold]", style=THEME.border))
    console.print()

    session = _mock_session()
    wd = str(_REPO_ROOT)

    # Welcome banner
    console.print(render_welcome_banner(session, wd, _get_version()))
    console.print()

    # Simulated user prompt bar
    console.print(
        render_user_prompt(
            "two checkout tests are failing. find the root cause, fix it, and run the tests"
        )
    )
    console.print()

    # Tool 1: bash (failing test run)
    console.print(
        render_tool_block(
            "bash",
            {"command": "pytest -q --tb=short"},
            {"exit_code": 1, "duration_seconds": 0.6},
            json.dumps(
                {
                    "exit_code": 1,
                    "output": (
                        "FF..\n"
                        "FAILED tests/test_pricing.py::test_bulk_discount_boundary - assert 100.0 == 90.0\n"
                        "FAILED tests/test_checkout.py::test_cart_total_at_ten - assert 250.0 == 225.0\n"
                        "2 failed, 2 passed in 0.42s"
                    ),
                }
            ),
            working_dir=wd,
        )
    )

    # Tool 2: compact read (no raw line dump)
    console.print(
        render_tool_block(
            "read",
            {"path": f"{wd}/src/pricing.py", "offset": 1, "limit": 45},
            {"start_line": 1, "end_line": 45, "total_lines": 112},
            None,
            working_dir=wd,
            elapsed=0.04,
        )
    )

    # Tool 3: edit with live colorized unified diff
    console.print(
        render_tool_block(
            "edit",
            {
                "path": f"{wd}/src/pricing.py",
                "old_string": (
                    "def apply_bulk_discount(qty: int, subtotal: float) -> float:\n"
                    "    if qty > BULK_THRESHOLD:\n"
                    "        return round(subtotal * (1 - BULK_RATE), 2)\n"
                    "    return subtotal"
                ),
                "new_string": (
                    "def apply_bulk_discount(qty: int, subtotal: float) -> float:\n"
                    "    # Discount applies inclusively at the threshold (>= 10 units)\n"
                    "    if qty >= BULK_THRESHOLD:\n"
                    "        return round(subtotal * (1 - BULK_RATE), 2)\n"
                    "    return subtotal"
                ),
            },
            {"path": f"{wd}/src/pricing.py"},
            f"Edited {wd}/src/pricing.py: exact match",
            working_dir=wd,
            elapsed=0.02,
        )
    )

    # Tool 4: subagent delegate
    console.print(
        render_tool_block(
            "delegate",
            {
                "instruction": "Verify discount boundary documentation and checkout edge cases",
                "model": "gpt-6.1-terra",
            },
            {"child_status": "completed", "duration_seconds": 3.8, "child_session_id": "51c09e7a"},
            "Checked README and checkout.py — docs already specify >= 10 units.",
            working_dir=wd,
        )
    )

    # Tool 5: bash passing tests
    console.print(
        render_tool_block(
            "bash",
            {"command": "pytest -q"},
            {"exit_code": 0, "duration_seconds": 0.4},
            json.dumps({"exit_code": 0, "output": "....\n4 passed in 0.36s"}),
            working_dir=wd,
        )
    )
    console.print()

    # Clear response divider + rendered Markdown
    console.print(render_agent_divider(after_tools=True))
    md_sample = (
        "## Root Cause & Fix\n\n"
        "In `src/pricing.py`, `apply_bulk_discount()` used strict `>` instead of `>=` against "
        "`BULK_THRESHOLD` (`10`), causing orders of exactly 10 units to miss the 10% discount.\n\n"
        "- Updated the boundary check to `qty >= BULK_THRESHOLD`\n"
        "- Confirmed docs and checkout totals match via subagent `#51c09e7a`\n"
        "- All **4 tests pass** (`pytest -q`)\n"
    )
    console.print(make_markdown(md_sample))

    # Turn footer
    console.print()
    console.print(
        render_turn_footer(
            {
                "context_size": 31_400,
                "total_input_tokens": 42_180,
                "total_output_tokens": 1_840,
                "total_cached_tokens": 34_200,
                "total_reasoning_tokens": 320,
                "total_cost_usd": 0.0184,
            },
            model="gpt-6.1-terra",
            elapsed=6.4,
        )
    )
    console.print()

    # Approval prompt card (high-risk & standard)
    panel, question = render_approval_card(
        "request_access",
        {
            "path": str(Path.home() / "proj" / "shared-lib"),
            "access": "write",
            "reason": "Update shared pricing constants to match the new threshold",
        },
        working_dir=wd,
    )
    console.print(panel)
    console.print(f"  {_markup(question, THEME.warning)}")
    console.print()

    # Single-shot header
    console.print(Text("Single-shot compact header:", style=THEME.muted))
    console.print(render_single_header(session, wd))
    console.print()

    # /status, /access, /help cards
    tok = TokenStatus(
        input_tokens=42_180,
        output_tokens=1_840,
        model="gpt-6.1-terra",
        access_level="workspace",
        cost_usd=0.0184,
    )
    tok.context_size = 31_400
    tok.total_cached_tokens = 34_200
    tok.total_reasoning_tokens = 320
    console.print(render_session_status_card(session, tok, wd))
    console.print()
    console.print(render_access_card(session))
    console.print()
    console.print(render_help_card())


def _create_mock_fleet(base_dir: Path) -> SessionStore:
    """Populate ``base_dir`` with realistic multi-project sessions, children, approvals, and traces."""
    now = datetime.now(UTC)
    pid = os.getpid()
    import socket

    host = socket.gethostname()

    def write_session(
        path: Path,
        *,
        sid: str,
        status: str,
        model: str,
        access: str,
        cwd: str,
        prompt: str,
        cost: float,
        started_ago: int,
        active_ago: int,
        parent_id: str | None = None,
        events: list[dict] | None = None,
        approval: dict | None = None,
    ) -> None:
        path.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": sid,
            "status": status,
            "model": model,
            "access": access,
            "cwd": cwd,
            "pid": pid,
            "host": host,
            "user": getpass.getuser(),
            "first_prompt": prompt,
            "cost_usd": cost,
            "started_at": (now - timedelta(seconds=started_ago)).isoformat(),
            "last_activity": (now - timedelta(seconds=active_ago)).isoformat(),
        }
        if parent_id:
            meta["parent_session_id"] = parent_id
        (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        if events:
            trace_file = path / "trace.jsonl"
            with open(trace_file, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
            ts = (now - timedelta(seconds=active_ago)).timestamp()
            os.utime(trace_file, (ts, ts))

        if approval:
            app_dir = path / "approvals"
            app_dir.mkdir(exist_ok=True)
            req_id = approval["id"]
            (app_dir / f"{req_id}.json").write_text(json.dumps(approval), encoding="utf-8")

    api_dir = str(Path.home() / "proj" / "api-server")
    shop_dir = str(Path.home() / "proj" / "shop")

    # 1. Running parent with two child subagents (one waiting_approval, one completed)
    write_session(
        base_dir / "a3f8c1d2",
        sid="a3f8c1d2",
        status="running",
        model="gpt-6.1-sol",
        access="workspace",
        cwd=api_dir,
        prompt="Migrate sync HTTP client to async httpx and update all callers",
        cost=0.42,
        started_ago=252,
        active_ago=3,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=250)).isoformat(),
                "prompt": "Migrate sync HTTP client to async httpx and update all callers",
            },
            {
                "type": "tool_start",
                "timestamp": (now - timedelta(seconds=210)).isoformat(),
                "tool_name": "grep",
                "tool_args": {"pattern": "SyncClient", "path": "src/"},
            },
            {
                "type": "tool_end",
                "timestamp": (now - timedelta(seconds=209)).isoformat(),
                "tool_name": "grep",
                "success": True,
            },
            {
                "type": "child_start",
                "timestamp": (now - timedelta(seconds=95)).isoformat(),
                "child_session_id": "51c09e7a",
                "tool_name": "delegate",
                "instruction": "Update billing/ callers to async and run billing tests",
            },
            {
                "type": "child_start",
                "timestamp": (now - timedelta(seconds=90)).isoformat(),
                "child_session_id": "9d4e2b10",
                "tool_name": "delegate",
                "instruction": "Audit auth middleware for missing await calls",
            },
            {
                "type": "child_end",
                "timestamp": (now - timedelta(seconds=42)).isoformat(),
                "child_session_id": "9d4e2b10",
                "status": "completed",
            },
            {
                "type": "tool_start",
                "timestamp": (now - timedelta(seconds=15)).isoformat(),
                "tool_name": "edit",
                "tool_args": {
                    "path": f"{api_dir}/src/client.py",
                    "old_string": "def fetch(self, url: str) -> Response:\n    return requests.get(url)",
                    "new_string": "async def fetch(self, url: str) -> Response:\n    return await self._http.get(url)",
                },
            },
            {
                "type": "tool_end",
                "timestamp": (now - timedelta(seconds=14)).isoformat(),
                "tool_name": "edit",
                "success": True,
            },
            {
                "type": "llm_end",
                "timestamp": (now - timedelta(seconds=6)).isoformat(),
                "input_tokens": 38_400,
                "output_tokens": 640,
                "cost_usd": 0.0142,
            },
            {
                "type": "message",
                "timestamp": (now - timedelta(seconds=4)).isoformat(),
                "role": "assistant",
                "content": "Core AsyncClient is migrated. Waiting on billing subagent #51c09e7a before running the full suite.",
            },
        ],
    )

    write_session(
        base_dir / "a3f8c1d2" / "children" / "51c09e7a",
        sid="51c09e7a",
        status="waiting_approval",
        model="gpt-6.1-terra",
        access="workspace",
        cwd=api_dir,
        parent_id="a3f8c1d2",
        prompt="Update billing/ callers to async and run billing tests",
        cost=0.08,
        started_ago=95,
        active_ago=18,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=95)).isoformat(),
                "prompt": "Update billing/ callers to async and run billing tests",
            },
            {
                "type": "approval_requested",
                "timestamp": (now - timedelta(seconds=18)).isoformat(),
                "request_id": "47e7d3",
                "tool_name": "request_access",
                "tool_args": {
                    "path": str(Path.home() / "proj" / "shared-schemas"),
                    "access": "write",
                    "reason": "Update shared billing DTO signature",
                },
            },
        ],
        approval={
            "id": "47e7d3",
            "session_id": "51c09e7a",
            "tool_name": "request_access",
            "tool_args": {
                "path": str(Path.home() / "proj" / "shared-schemas"),
                "access": "write",
                "reason": "Update shared billing DTO signature",
            },
            "requested_at": (now - timedelta(seconds=18)).isoformat(),
        },
    )

    write_session(
        base_dir / "a3f8c1d2" / "children" / "9d4e2b10",
        sid="9d4e2b10",
        status="completed",
        model="gpt-6.1-terra",
        access="read-only",
        cwd=api_dir,
        parent_id="a3f8c1d2",
        prompt="Audit auth middleware for missing await calls",
        cost=0.04,
        started_ago=90,
        active_ago=42,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=90)).isoformat(),
                "prompt": "Audit auth middleware for missing await calls",
            },
            {
                "type": "run_end",
                "timestamp": (now - timedelta(seconds=42)).isoformat(),
                "status": "completed",
            },
        ],
    )

    # 2. Paused session in api-server
    write_session(
        base_dir / "c81b04ef",
        sid="c81b04ef",
        status="paused",
        model="gpt-6.1-terra",
        access="workspace",
        cwd=api_dir,
        prompt="Refactor webhook retry queue with exponential backoff",
        cost=0.19,
        started_ago=610,
        active_ago=125,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=610)).isoformat(),
                "prompt": "Refactor webhook retry queue with exponential backoff",
            },
            {
                "type": "tool_start",
                "timestamp": (now - timedelta(seconds=140)).isoformat(),
                "tool_name": "bash",
                "tool_args": {"command": "pytest tests/test_webhooks.py"},
            },
            {
                "type": "tool_end",
                "timestamp": (now - timedelta(seconds=132)).isoformat(),
                "tool_name": "bash",
                "success": True,
            },
        ],
    )
    (base_dir / "c81b04ef" / "pause").touch()

    # 3. Interactive idle session in ~/proj/shop
    write_session(
        base_dir / "7b2e09aa",
        sid="7b2e09aa",
        status="waiting_input",
        model="gpt-6.1-terra",
        access="workspace",
        cwd=shop_dir,
        prompt="Two checkout tests are failing — find root cause and fix",
        cost=0.11,
        started_ago=480,
        active_ago=35,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=480)).isoformat(),
                "prompt": "Two checkout tests are failing — find root cause and fix",
            },
            {
                "type": "message",
                "timestamp": (now - timedelta(seconds=35)).isoformat(),
                "role": "assistant",
                "content": "Fixed the bulk discount boundary in pricing.py:12. All 4 checkout tests pass.",
            },
            {
                "type": "run_end",
                "timestamp": (now - timedelta(seconds=35)).isoformat(),
                "status": "completed",
            },
        ],
    )

    # 4. Waiting approval session on unsandboxed docker command in ~/proj/shop
    write_session(
        base_dir / "f019a2bb",
        sid="f019a2bb",
        status="waiting_approval",
        model="gpt-6.1-sol",
        access="workspace",
        cwd=shop_dir,
        prompt="Run integration tests against local postgres container",
        cost=0.06,
        started_ago=180,
        active_ago=52,
        events=[
            {
                "type": "run_start",
                "timestamp": (now - timedelta(seconds=180)).isoformat(),
                "prompt": "Run integration tests against local postgres container",
            },
            {
                "type": "approval_requested",
                "timestamp": (now - timedelta(seconds=52)).isoformat(),
                "request_id": "9c21ab",
                "tool_name": "bash",
                "tool_args": {
                    "command": "docker compose up -d postgres",
                    "unsandboxed": True,
                    "reason": "Need Docker daemon socket to start test database",
                },
            },
        ],
        approval={
            "id": "9c21ab",
            "session_id": "f019a2bb",
            "tool_name": "bash",
            "tool_args": {
                "command": "docker compose up -d postgres",
                "unsandboxed": True,
                "reason": "Need Docker daemon socket to start test database",
            },
            "requested_at": (now - timedelta(seconds=52)).isoformat(),
        },
    )

    return SessionStore(base_dir)


def preview_monitor(*, interactive_live: bool = False) -> None:
    """Render monitor shell commands + Mission Control dashboard on a mock fleet."""
    with tempfile.TemporaryDirectory(prefix="rho-ui-preview-") as tmp:
        base_dir = Path(tmp)
        store = _create_mock_fleet(base_dir)

        if interactive_live:
            MonitorSession(base_dir).run(start_live=True)
            return

        console.print(Rule("[bold]2 · Monitor CLI (rho-agent monitor)[/bold]", style=THEME.border))
        console.print()

        mon = MonitorSession(base_dir)
        mon._print_banner()
        console.print()
        mon._cmd_ps()
        console.print()
        mon._cmd_approvals()
        console.print()
        mon._cmd_inspect("a3f8c1d2")
        console.print()

        console.print(
            Rule(
                "[bold]3 · Mission Control Live Dashboard (rho-agent monitor --live)[/bold]",
                style=THEME.border,
            )
        )
        console.print()
        mc = MissionControl(store)
        console.print(mc.build_layout(), height=26)


def main() -> None:
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    if mode == "cli":
        preview_cli()
    elif mode == "monitor":
        preview_monitor(interactive_live=False)
    elif mode == "live":
        preview_monitor(interactive_live=True)
    else:
        preview_cli()
        console.print()
        preview_monitor(interactive_live=False)


if __name__ == "__main__":
    main()
