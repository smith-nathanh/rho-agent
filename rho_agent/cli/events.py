"""Event handling: ApprovalHandler, handle_event, handle_command, switch_runtime_profile."""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from ..capabilities import CapabilityProfile
from ..core.events import AgentEvent
from .theme import THEME
from .errors import InvalidProfileError
from .formatting import (
    TokenStatus,
    _format_tool_preview,
    _format_tool_signature,
    _format_tool_summary,
    _markup,
)
from .state import MARKDOWN_THEME, console


class ApprovalHandler:
    """Handles command approval prompts with Rich UI."""

    def __init__(self, auto_approve: bool = False) -> None:
        self.auto_approve = auto_approve

    def enable_auto_approve(self) -> None:
        """Enable auto-approve mode for this session."""
        self.auto_approve = True
        console.print(_markup("Auto-approve enabled for this session", THEME.success))

    async def check_approval(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Prompt user for approval. Returns True if approved."""
        if self.auto_approve:
            return True

        sig = _format_tool_signature(tool_name, tool_args)
        console.print(
            Panel(
                _markup(sig, THEME.tool_call),
                title=_markup("approval", THEME.warning),
                title_align="left",
                border_style=THEME.warning,
                padding=(0, 1),
            )
        )
        console.print(_markup("Approve? \\[Y/n]:", THEME.warning), end=" ")

        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        # Default to yes (empty input = approve)
        return response not in ("n", "no")


def handle_event(
    event: AgentEvent,
    *,
    show_turn_usage: bool = True,
    token_status: TokenStatus | None = None,
    render_markdown: bool = False,
    pending_text_chunks: list[str] | None = None,
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
                console.print(
                    Markdown(
                        combined,
                        style=THEME.primary,
                        code_theme="ansi_dark",
                        inline_code_theme="ansi_dark",
                    )
                )
        except Exception:
            print(combined, flush=True)
        pending_text_chunks.clear()

    if event.type in (
        "tool_start",
        "tool_end",
        "tool_blocked",
        "compact_start",
        "compact_end",
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
        # Show compact tool signature (like Claude Code)
        sig = _format_tool_signature(event.tool_name, event.tool_args)
        console.print()
        console.print(
            Panel(
                _markup(sig, THEME.tool_call),
                title=_markup("tool", THEME.secondary),
                title_align="left",
                border_style=THEME.secondary,
                padding=(0, 1),
            )
        )
    elif event.type == "tool_end":
        # Show a brief summary of what the tool found
        summary = _format_tool_summary(event.tool_name, event.tool_metadata, event.tool_result)
        preview = _format_tool_preview(event.tool_result, event.tool_name)
        body_lines: list[str] = []
        if summary:
            body_lines.append(_markup(f"-> {summary}", THEME.success))
        if preview:
            body_lines.append(_markup(preview, THEME.tool_result))
        if body_lines:
            console.print(
                Panel(
                    "\n".join(body_lines),
                    border_style=THEME.border,
                    padding=(0, 1),
                )
            )

    elif event.type == "tool_blocked":
        console.print(_markup("Command rejected", THEME.error))

    elif event.type == "compact_start":
        trigger = event.content or "manual"
        if trigger == "auto":
            console.print(_markup("Context limit approaching, auto-compacting...", THEME.warning))
        else:
            console.print(_markup("Compacting conversation...", THEME.warning))

    elif event.type == "compact_end":
        console.print(_markup(event.content or "", THEME.success))
        console.print(
            _markup(
                "Note: Multiple compactions can reduce accuracy. "
                "Start a new session when possible.",
                THEME.muted,
            )
        )

    elif event.type == "turn_complete":
        # Ensure we end on a new line after streamed text
        if not render_markdown:
            print()
        if token_status:
            token_status.update(event.usage)
        if show_turn_usage:
            usage = event.usage or {}
            context_size = usage.get("context_size", 0)
            total_in = usage.get("total_input_tokens", 0)
            total_out = usage.get("total_output_tokens", 0)
            total_cached = usage.get("total_cached_tokens", 0)
            total_cost = usage.get("total_cost_usd", 0.0)
            cache_str = ""
            if total_cached and total_in:
                cache_str = f", cache: {total_cached / total_in:.0%}"
            cost_str = f", cost: ${total_cost:.4f}" if total_cost else ""
            print(f"[context: {context_size}{cache_str}, session: {total_in} in | {total_out} out{cost_str}]")

    elif event.type == "error":
        console.print(_markup(f"Error: {event.content}", THEME.error))


def handle_command(
    cmd: str,
    approval_handler: ApprovalHandler,
) -> str | None:
    """Handle slash commands.

    Returns:
        None to continue loop normally
        "compact" or "compact:<instructions>" if /compact was called
        Other string values for future special handling
    """
    if cmd == "/approve":
        approval_handler.enable_auto_approve()
        return None

    if cmd.startswith("/compact"):
        # Extract optional instructions after /compact
        parts = cmd.split(maxsplit=1)
        if len(parts) > 1:
            return f"compact:{parts[1]}"
        return "compact"

    if cmd.startswith("/write"):
        # Handled in run_interactive (needs registry/profile context)
        return "write"

    if cmd.startswith("/mode"):
        # Handled in run_interactive (needs runtime/profile context)
        return "mode"

    if cmd.startswith("/resume"):
        # Handled in run_interactive (needs conversation store/session context)
        return "resume"

    if cmd == "/help":

        def line(lhs: str, rhs: str) -> str:
            # Pad using visible text width, then escape for Rich markup safety.
            safe_lhs = escape(lhs.ljust(28))
            safe_rhs = escape(rhs)
            return f"  {safe_lhs}- {safe_rhs}"

        help_text = "\n".join(
            [
                "[bold]Commands:[/bold]",
                line("/approve", "Enable auto-approve for all tool calls"),
                line("/compact [guidance]", "Compact conversation history"),
                line("/mode [name|path|status]", "Switch or show active capability mode"),
                line("/write [on|off|status]", "Toggle create-only write tool (readonly mode)"),
                line("/resume [latest|id]", "Resume a saved conversation"),
                line("/help", "Show this help"),
                line("/clear", "Clear the screen"),
                line("exit", "Quit the session"),
                "",
                "[bold]Input:[/bold]",
                line("Enter", "Send message"),
                line("Esc+Enter", "New line"),
                "",
                "[bold]Conversations:[/bold]",
                line("rho-agent --list", "List saved conversations"),
                line("rho-agent -r latest", "Resume most recent conversation"),
                line("rho-agent -r <id>", "Resume specific conversation"),
            ]
        )
        console.print(
            Panel(
                help_text,
                title="Help",
                title_align="left",
                border_style=THEME.border,
            )
        )
        return None

    if cmd == "/clear":
        console.clear()
        return None

    return None


def switch_runtime_profile(
    profile_name_or_path: str,
    *,
    working_dir: str,
) -> CapabilityProfile:
    """Load a capability profile (used for mode switching in interactive sessions)."""
    from ..capabilities.factory import load_profile

    try:
        return load_profile(profile_name_or_path)
    except (ValueError, FileNotFoundError) as e:
        raise InvalidProfileError(str(e)) from e
