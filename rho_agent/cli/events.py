"""Event handling: ApprovalHandler, handle_event, handle_command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich import box
from rich.panel import Panel

from ..core.approvals import ApprovalQueue, ApprovalRequest
from ..core.events import AgentEvent
from .formatting import TokenStatus, _markup
from .state import MARKDOWN_THEME, console
from .theme import THEME
from .ui import (
    make_markdown,
    render_approval_card,
    render_help_card,
    render_tool_end,
    render_tool_start,
    render_turn_footer,
)


class ApprovalHandler:
    """Asks the user to approve requests from the agent, in the terminal or
    (with ``queue`` set) through ``rho-agent monitor``.

    Requests are access to a directory outside the sandbox (``request_access``),
    a command to run outside it (``bash`` with ``unsandboxed``), or a custom
    tool that declares ``requires_approval``.
    """

    def __init__(self) -> None:
        # When set, requests go to the monitor instead of this terminal.
        self.queue: ApprovalQueue | None = None
        # Optional hooks called around interactive prompts or queued announcements
        # (used by TurnRenderer to pause and resume live spinners cleanly).
        self.before_prompt: Callable[[], None] | None = None
        self.after_prompt: Callable[[], None] | None = None
        self.working_dir: str | None = None

    def announce_queued(self, request: ApprovalRequest) -> None:
        """Pause any active live display and announce a queued approval."""
        if self.before_prompt is not None:
            self.before_prompt()
        announce_queued_approval(request, working_dir=self.working_dir)

    async def check_approval(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Ask the user. Returns True if approved."""
        if self.queue is not None:
            approved = await self.queue(tool_name, tool_args)
            if approved and self.after_prompt is not None:
                self.after_prompt()
            return approved

        if self.before_prompt is not None:
            self.before_prompt()

        panel, question = render_approval_card(tool_name, tool_args, working_dir=self.working_dir)
        console.print()
        console.print(panel)
        console.print(f"  {_markup(question, THEME.warning)}", end=" ")

        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        approved = response in ("y", "yes")
        if approved and self.after_prompt is not None:
            self.after_prompt()
        return approved


def announce_queued_approval(
    request: ApprovalRequest,
    *,
    working_dir: str | None = None,
) -> None:
    """Tell the user a tool call is waiting for an answer in the monitor."""
    panel, _ = render_approval_card(
        request.tool_name,
        request.tool_args,
        request_id=request.id,
        working_dir=working_dir,
    )
    console.print()
    console.print(panel)
    console.print(
        _markup(
            f"  Answer from another terminal: rho-agent monitor → "
            f"'approve {request.id}' or 'deny {request.id}'",
            THEME.muted,
        )
    )


def handle_event(
    event: AgentEvent,
    *,
    show_turn_usage: bool = True,
    token_status: TokenStatus | None = None,
    render_markdown: bool = False,
    pending_text_chunks: list[str] | None = None,
    working_dir: str | None = None,
    model: str = "",
) -> None:
    """Handle an agent event by printing to console."""

    def flush_markdown() -> None:
        if not render_markdown or pending_text_chunks is None:
            return
        combined = "".join(pending_text_chunks).strip()
        if not combined:
            pending_text_chunks.clear()
            return
        try:
            with console.use_theme(MARKDOWN_THEME):
                console.print(make_markdown(combined))
        except Exception:
            print(combined, flush=True)
        pending_text_chunks.clear()

    if event.type in (
        "tool_start",
        "tool_end",
        "tool_blocked",
        "compact_start",
        "compact_end",
        "retry",
        "turn_complete",
        "error",
        "cancelled",
    ):
        flush_markdown()

    if event.type == "text":
        if render_markdown and pending_text_chunks is not None:
            pending_text_chunks.append(event.content or "")
        else:
            # Stream text immediately as it arrives
            print(event.content or "", end="", flush=True)

    elif event.type == "tool_start":
        console.print()
        console.print(render_tool_start(event.tool_name, event.tool_args, working_dir=working_dir))

    elif event.type == "tool_end":
        block = render_tool_end(event.tool_name, event.tool_metadata, event.tool_result)
        if block is not None:
            console.print(block)

    elif event.type == "tool_blocked":
        console.print(
            f"  [{THEME.rail}]└[/{THEME.rail}] {_markup('rejected by user', THEME.warning)}"
        )

    elif event.type == "retry":
        console.print(_markup(f"  retrying ({event.content or 'transient error'})", THEME.warning))

    elif event.type == "compact_start":
        trigger = event.content or "manual"
        if trigger == "auto":
            console.print(_markup("  Context limit approaching — auto-compacting…", THEME.warning))
        else:
            console.print(_markup("  Compacting conversation…", THEME.warning))

    elif event.type == "compact_end":
        console.print(_markup(f"  {event.content or 'Compacted'}", THEME.secondary))
        console.print(
            _markup(
                "  Tip: start a fresh session when switching tasks for best accuracy.",
                THEME.muted,
            )
        )

    elif event.type == "turn_complete":
        if not render_markdown:
            print()
        if token_status:
            token_status.update(event.usage)
        if show_turn_usage:
            console.print()
            console.print(render_turn_footer(event.usage, model=model))

    elif event.type == "error":
        console.print(
            Panel(
                _markup(str(event.content or "Unknown error"), THEME.error),
                title=_markup("Error", THEME.error),
                title_align="left",
                box=box.SQUARE,
                border_style=THEME.error,
                padding=(0, 1),
            )
        )


def handle_command(
    cmd: str,
    approval_handler: ApprovalHandler,
) -> str | None:
    """Handle slash commands.

    Returns:
        None to continue loop normally
        "compact" or "compact:<instructions>" if /compact was called
        Other string values for special handling in InteractiveSession
    """
    if cmd.startswith("/compact"):
        parts = cmd.split(maxsplit=1)
        if len(parts) > 1:
            return f"compact:{parts[1]}"
        return "compact"

    if cmd.startswith("/access"):
        return "access"

    if cmd.startswith("/resume"):
        return "resume"

    if cmd == "/status":
        return "status"

    if cmd == "/help":
        console.print(render_help_card())
        return None

    if cmd == "/clear":
        console.clear()
        return None

    return None
