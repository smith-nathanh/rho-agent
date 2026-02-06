"""CLI entry point for rho-agent."""

import asyncio
import importlib.metadata
import os
import platform
import re
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Iterable, Optional

import typer
import yaml
from dotenv import load_dotenv

# Load .env before anything else so env vars are available for defaults
load_dotenv()
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import (
    Completer,
    Completion,
    WordCompleter,
    merge_completers,
)
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from .client.model import ModelClient
from .core.agent import Agent, AgentEvent
from .core.conversations import ConversationStore
from .core.session import Session
from .prompts import load_prompt, parse_vars, prepare_prompt
from .capabilities import CapabilityProfile, ShellMode
from .capabilities.factory import ToolFactory, load_profile
from .tools.registry import ToolRegistry
from .ui.theme import THEME
from .cli_errors import (
    MissingApiKeyError,
    InvalidProfileError,
    InvalidModeError,
    PromptLoadError,
)
from .observability.config import ObservabilityConfig, DEFAULT_TELEMETRY_DB
from .observability.processor import ObservabilityProcessor
from .signals import AgentInfo, SignalManager

# Config directory for rho-agent data
CONFIG_DIR = Path.home() / ".config" / "rho-agent"
HISTORY_FILE = CONFIG_DIR / "history"
DEFAULT_PROMPT_FILE = CONFIG_DIR / "default.md"

# Built-in default prompt (ships with package)
BUILTIN_PROMPT_FILE = Path(__file__).parent / "prompts" / "default.md"

# Tool output preview lines (0 to disable)
TOOL_PREVIEW_LINES = int(os.getenv("RHO_AGENT_PREVIEW_LINES", "6"))

# Rich console for all output
console = Console()

# Typer app
app = typer.Typer(
    name="rho-agent",
    help="An agent harness and CLI with readonly and developer modes.",
    epilog=(
        "Examples:\n"
        "  rho-agent\n"
        "  rho-agent \"What errors are in app.log?\"\n"
        "  rho-agent --prompt \"Investigate why CI is failing\"\n"
        "  rho-agent --profile readonly\n"
        "  rho-agent --profile developer\n"
        "  rho-agent -r latest\n"
        "  rho-agent --system-prompt ./prompt.md --var env=prod"
    ),
    add_completion=False,
)


def _markup(text: str, color: str) -> str:
    # Escape user/model/tool text so Rich markup tags inside content
    # don't get interpreted as formatting directives.
    return f"[{color}]{escape(text)}[/{color}]"


def _is_interactive_terminal() -> bool:
    return console.is_terminal and sys.stdin.isatty() and sys.stdout.isatty()


def _get_version() -> str:
    try:
        return importlib.metadata.version("rho-agent")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class TokenStatus:
    """Session token metrics for persistent status display."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.context_size = 0
        self.total_input_tokens = input_tokens
        self.total_output_tokens = output_tokens

    def update(self, usage: dict[str, int] | None) -> None:
        if not usage:
            return
        self.context_size = usage.get("context_size", self.context_size)
        self.total_input_tokens = usage.get(
            "total_input_tokens", self.total_input_tokens
        )
        self.total_output_tokens = usage.get(
            "total_output_tokens", self.total_output_tokens
        )

    def render(self) -> str:
        return (
            f"context:{self.context_size} "
            f"in:{self.total_input_tokens} "
            f"out:{self.total_output_tokens}"
        )

# Commands the user can type during the session
COMMANDS = [
    "/approve",
    "/compact",
    "/write",
    "/resume",
    "/help",
    "/clear",
    "exit",
    "quit",
]

# Pattern to detect path-like strings in text
PATH_PATTERN = re.compile(
    r"(~/?|\.{1,2}/|/)?([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-.]*|~[a-zA-Z0-9_\-./]*)$"
)


class InlinePathCompleter(Completer):
    """Completes file paths that appear anywhere in the input text."""

    def __init__(self, working_dir: str | None = None) -> None:
        self.working_dir = Path(working_dir).expanduser() if working_dir else Path.cwd()

    def get_completions(
        self, document: Document, complete_event: Any
    ) -> Iterable[Completion]:
        text_before_cursor = document.text_before_cursor

        match = PATH_PATTERN.search(text_before_cursor)
        if not match:
            return

        path_text = match.group(0)
        start_pos = -len(path_text)

        # Expand paths for lookup
        if path_text.startswith("~"):
            expanded = os.path.expanduser(path_text)
        elif path_text.startswith("/"):
            expanded = path_text
        else:
            expanded = str(self.working_dir / path_text)

        path = Path(expanded)
        if expanded.endswith("/"):
            parent = path
            prefix = ""
        else:
            parent = path.parent
            prefix = path.name

        try:
            if not parent.exists():
                return

            for entry in sorted(parent.iterdir()):
                name = entry.name
                if not name.startswith(prefix):
                    continue
                if name.startswith(".") and not prefix.startswith("."):
                    continue

                # Build completion text preserving user's path style
                if path_text.startswith("~"):
                    if expanded.endswith("/"):
                        completion_text = path_text + name
                    else:
                        completion_text = (
                            path_text.rsplit("/", 1)[0] + "/" + name
                            if "/" in path_text
                            else "~/" + name
                        )
                else:
                    if expanded.endswith("/"):
                        completion_text = path_text + name
                    else:
                        completion_text = (
                            str(path.parent / name) if "/" in path_text else name
                        )

                display = name + "/" if entry.is_dir() else name
                if entry.is_dir():
                    completion_text += "/"

                yield Completion(
                    completion_text,
                    start_position=start_pos,
                    display=display,
                    display_meta="dir" if entry.is_dir() else "",
                )
        except PermissionError:
            return


def create_completer(working_dir: str | None = None) -> Completer:
    """Create a merged completer for commands and paths."""
    command_completer = WordCompleter(COMMANDS, ignore_case=True)
    path_completer = InlinePathCompleter(working_dir=working_dir)
    return merge_completers([command_completer, path_completer])


def create_registry(
    working_dir: str | None = None,
    profile: CapabilityProfile | None = None,
) -> ToolRegistry:
    """Create and configure the tool registry.

    Args:
        working_dir: Working directory for shell commands.
        profile: Capability profile to use. Defaults to readonly profile.

    Returns:
        Configured tool registry.
    """
    if profile is None:
        profile = CapabilityProfile.readonly()

    factory = ToolFactory(profile)
    return factory.create_registry(working_dir=working_dir)


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

        console.print(_markup("Approve? \\[Y/n]:", THEME.warning), end=" ")

        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        # Default to yes (empty input = approve)
        return response not in ("n", "no")


def _format_tool_signature(tool_name: str, tool_args: dict[str, Any] | None) -> str:
    """Format tool call as a signature like: read(path='/foo/bar.py')"""
    if not tool_args:
        return f"{tool_name}()"

    # For bash commands, show just the command
    if tool_name == "bash" and "command" in tool_args:
        return f"{tool_name}({tool_args['command']})"

    # For other tools, show all args (no truncation)
    parts = []
    for key, val in tool_args.items():
        if isinstance(val, str):
            parts.append(f"{key}='{val}'")
        else:
            parts.append(f"{key}={val}")

    return f"{tool_name}({', '.join(parts)})"


def _format_tool_summary(
    tool_name: str | None,
    metadata: dict[str, Any] | None,
    result: str | None,
) -> str | None:
    """Format a brief summary of tool results."""
    if not tool_name:
        return None

    # Use metadata if available
    if metadata:
        # grep tool
        if tool_name == "grep":
            matches = metadata.get("matches", 0)
            truncated = metadata.get("truncated", False)
            if matches:
                suffix = "+" if truncated else ""
                return f"{matches}{suffix} matches"
            return "No matches"

        # read tool
        if tool_name == "read":
            total = metadata.get("total_lines", 0)
            start = metadata.get("start_line", 1)
            end = metadata.get("end_line", total)
            if total:
                return f"Read lines {start}-{end} of {total}"

        # list tool
        if tool_name == "list":
            count = metadata.get("item_count", 0)
            if count:
                return f"{count} items"

        # write tool
        if tool_name == "write":
            size = metadata.get("size_bytes", 0)
            lines = metadata.get("lines", 0)
            if size:
                return f"Wrote {size} bytes ({lines} lines)"

        # glob tool
        if tool_name == "glob":
            matches = metadata.get("matches", 0)
            total = metadata.get("total", matches)
            if matches:
                if total > matches:
                    return f"{matches} of {total} files"
                return f"{matches} files"
            return "No files found"

        # Database tools
        if tool_name in ("oracle", "sqlite", "vertica", "mysql", "postgres"):
            rows = metadata.get("row_count", metadata.get("table_count", 0))
            if rows:
                return f"{rows} rows"

    # Fallback: count lines in result
    if result:
        lines = result.count("\n") + 1
        if lines > 1:
            return f"{lines} lines"

    return None


def _format_tool_preview(
    result: str | None, max_lines: int | None = None
) -> str | None:
    """Get first N lines of tool output as a preview."""
    if max_lines is None:
        max_lines = TOOL_PREVIEW_LINES

    if not result or max_lines <= 0:
        return None

    lines = result.split("\n")
    if len(lines) <= max_lines:
        return result

    preview_lines = lines[:max_lines]
    remaining = len(lines) - max_lines
    preview_lines.append(f"... ({remaining} more lines)")
    return "\n".join(preview_lines)


def handle_event(
    event: AgentEvent,
    *,
    show_turn_usage: bool = True,
    token_status: TokenStatus | None = None,
) -> None:
    """Handle an agent event by printing to console."""
    if event.type == "text":
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
                border_style=THEME.secondary,
                padding=(0, 1),
            )
        )

    elif event.type == "tool_end":
        # Show a brief summary of what the tool found
        summary = _format_tool_summary(
            event.tool_name, event.tool_metadata, event.tool_result
        )
        preview = _format_tool_preview(event.tool_result)
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
            console.print(
                _markup("Context limit approaching, auto-compacting...", THEME.warning)
            )
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
        print()
        if token_status:
            token_status.update(event.usage)
        if show_turn_usage:
            usage = event.usage or {}
            context_size = usage.get("context_size", 0)
            total_in = usage.get("total_input_tokens", 0)
            total_out = usage.get("total_output_tokens", 0)
            print(f"[context: {context_size}, total: {total_in} in, {total_out} out]")

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
                border_style=THEME.border,
            )
        )
        return None

    if cmd == "/clear":
        console.clear()
        return None

    return None


async def run_interactive(
    agent: Agent,
    approval_handler: ApprovalHandler,
    session: Session,
    model: str,
    mode_name: str,
    registry: ToolRegistry,
    working_dir: str,
    conversation_store: ConversationStore,
    session_started: datetime,
    conversation_id: str | None = None,
    observability: ObservabilityProcessor | None = None,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run an interactive REPL session."""
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Start observability session if enabled
    if observability:
        await observability.start_session()

    token_status = TokenStatus(
        input_tokens=session.total_input_tokens,
        output_tokens=session.total_output_tokens,
    )
    interactive_tty = _is_interactive_terminal()

    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def _(event: Any) -> None:
        event.app.current_buffer.validate_and_handle()

    @key_bindings.add("escape", "enter")
    def _(event: Any) -> None:
        event.app.current_buffer.insert_text("\n")

    # Prompt toolkit session with history and completion
    prompt_session: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=create_completer(working_dir=working_dir),
        multiline=True,
        key_bindings=key_bindings,
        bottom_toolbar=(lambda: token_status.render()),
        complete_while_typing=False,
        complete_in_thread=True,
    )

    # Welcome message
    obs_status = (
        _markup("telemetry enabled", THEME.success)
        if observability
        else _markup("telemetry off", THEME.muted)
    )
    version = _get_version()
    console.print(
        Panel(
            f"[bold]{_markup('ρ rho-agent', THEME.primary)}[/bold] v{version}\n"
            f"Mode: {_markup(mode_name, THEME.accent)}\n"
            f"Model: {_markup(model, THEME.accent)} {obs_status}\n"
            "Enter to send, Esc+Enter for newline, Ctrl+C to cancel.\n"
            "Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit.",
            border_style=THEME.border,
        )
    )

    session_status = "completed"

    def handle_file_write_toggle(cmd: str) -> None:
        if mode_name != "readonly":
            console.print(
                _markup(
                    "File write toggling is only available in readonly mode.",
                    THEME.warning,
                )
            )
            return

        parts = cmd.split()
        has_write = "write" in registry

        if len(parts) == 1:
            if has_write:
                console.print(
                    _markup(
                        "File write is ON (create-only, approval required).",
                        THEME.success,
                    )
                )
                return
            console.print(
                _markup(
                    "Enable file write for exports? \\[y/N]:",
                    THEME.warning,
                ),
                end=" ",
            )
            try:
                response = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = "n"
            target = "on" if response in ("y", "yes") else "status"
        else:
            target = parts[1].lower()

        if target in ("status",):
            status = "ON" if has_write else "OFF"
            color = THEME.success if has_write else THEME.muted
            console.print(_markup(f"File write is {status}.", color))
            return

        if target in ("on", "enable"):
            if has_write:
                console.print(_markup("File write is already ON.", THEME.muted))
                return
            from .tools.handlers.write import WriteHandler

            registry.register(WriteHandler(create_only=True, requires_approval=True))
            console.print(
                _markup(
                    "File write enabled for this session (create-only, approval required).",
                    THEME.success,
                )
            )
            return

        if target in ("off", "disable"):
            if not has_write:
                console.print(_markup("File write is already OFF.", THEME.muted))
                return
            registry.unregister("write")
            console.print(_markup("File write disabled for this session.", THEME.warning))
            return

        console.print(
            _markup(
                "Usage: /write [on|off|status]",
                THEME.warning,
            )
        )

    def _resolve_resume_id(raw: str, conversations: list[Any]) -> str | None:
        target = raw.strip()
        if not target:
            return None
        # Numeric selection from /resume list (1-based)
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(conversations):
                return conversations[idx - 1].id
            return None
        if target.lower() == "latest":
            return conversations[0].id if conversations else None
        # Exact ID first
        for conv in conversations:
            if conv.id == target:
                return conv.id
        # Prefix match
        matches = [conv.id for conv in conversations if conv.id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        return None

    def handle_resume(cmd: str) -> None:
        nonlocal conversation_id, session_started

        conversations = conversation_store.list_conversations(limit=20)
        if not conversations:
            console.print(_markup("No saved conversations to resume.", THEME.error))
            return

        parts = cmd.split(maxsplit=1)
        selected_id: str | None = None

        if len(parts) > 1:
            selected_id = _resolve_resume_id(parts[1], conversations)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve conversation '{parts[1]}'. "
                        "Use /resume to list and select.",
                        THEME.warning,
                    )
                )
                return
        else:
            console.print(_markup("Recent conversations:", THEME.secondary))
            for idx, conv in enumerate(conversations, start=1):
                try:
                    started_dt = datetime.fromisoformat(conv.started)
                    time_str = started_dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    time_str = conv.id
                console.print(
                    f"{_markup(f'{idx:>2}.', THEME.secondary)} "
                    f"{_markup(conv.id, THEME.accent)}  {time_str}  "
                    f"{_markup(conv.model, THEME.muted)}"
                )
                if conv.display_preview:
                    console.print(_markup(f"  {conv.display_preview}", THEME.muted))
            console.print()
            console.print(
                _markup(
                    "Enter number, conversation ID, or prefix (blank to cancel):",
                    THEME.warning,
                ),
                end=" ",
            )
            try:
                raw = input().strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            if not raw:
                console.print(_markup("Resume cancelled.", THEME.muted))
                return
            selected_id = _resolve_resume_id(raw, conversations)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve conversation '{raw}'.",
                        THEME.error,
                    )
                )
                return

        resumed = conversation_store.load(selected_id)
        if not resumed:
            console.print(
                _markup(f"Conversation not found: {selected_id}", THEME.error)
            )
            return

        session.system_prompt = resumed.system_prompt
        session.history = resumed.history.copy()
        session.total_input_tokens = resumed.input_tokens
        session.total_output_tokens = resumed.output_tokens
        token_status.context_size = 0
        token_status.total_input_tokens = resumed.input_tokens
        token_status.total_output_tokens = resumed.output_tokens
        conversation_id = resumed.id
        try:
            session_started = datetime.fromisoformat(resumed.started)
        except ValueError:
            session_started = datetime.now()

        console.print(
            _markup(f"Resumed conversation: {resumed.id}", THEME.success)
        )
        console.print(
            _markup(
                f"Messages: {len(resumed.history)}  "
                f"tokens in/out: {resumed.input_tokens}/{resumed.output_tokens}",
                THEME.muted,
            )
        )

    try:
        while True:
            try:
                console.print()
                user_input = await prompt_session.prompt_async(
                    HTML(f"<style fg='{THEME.prompt}'><b>&gt;</b></style> ")
                )
                user_input = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                break

            if user_input.startswith("/"):
                if user_input.startswith("/write"):
                    handle_file_write_toggle(user_input)
                    continue
                if user_input.startswith("/resume"):
                    handle_resume(user_input)
                    continue

                action = handle_command(user_input, approval_handler)
                if action and action.startswith("compact"):
                    # Handle /compact command
                    instructions = ""
                    if ":" in action:
                        instructions = action.split(":", 1)[1]
                    handle_event(AgentEvent(type="compact_start", content="manual"))
                    result = await agent.compact(
                        custom_instructions=instructions, trigger="manual"
                    )
                    handle_event(
                        AgentEvent(
                            type="compact_end",
                            content=f"Compacted: {result.tokens_before} → {result.tokens_after} tokens",
                        )
                    )
                continue

            # Run the turn and handle events with cancellation support
            loop = asyncio.get_event_loop()

            def on_cancel():
                console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
                agent.request_cancel()

            # Register signal handler for this turn (Unix only)
            if platform.system() != "Windows":
                loop.add_signal_handler(signal.SIGINT, on_cancel)

            try:
                # Wrap event stream with observability if enabled
                events = agent.run_turn(user_input)
                if observability:
                    events = observability.wrap_turn(events, user_input)

                status_ctx = None
                start = monotonic()
                if interactive_tty:
                    status_ctx = console.status(
                        "⠋ working (0s)",
                        spinner="dots",
                        spinner_style=THEME.accent,
                    )
                    status_ctx.__enter__()
                saw_model_output = False

                async for event in events:
                    if status_ctx and not saw_model_output:
                        elapsed = int(monotonic() - start)
                        status_ctx.update(
                            f"⠋ working ({elapsed}s • Ctrl+C: cancel)"
                        )
                    if event.type in ("text", "tool_start", "error", "cancelled"):
                        saw_model_output = True
                        if status_ctx:
                            status_ctx.__exit__(None, None, None)
                            status_ctx = None
                    if event.type == "cancelled":
                        # Check if this was an external kill signal
                        if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                            session_status = "cancelled"
                            if observability:
                                observability.context.metadata["cancel_source"] = "kill_command"
                            console.print(_markup("Killed by rho-agent kill", THEME.warning))
                        else:
                            console.print(_markup("Turn cancelled", THEME.muted))
                        break
                    handle_event(event, show_turn_usage=False, token_status=token_status)
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
            finally:
                # Remove signal handler after turn
                if platform.system() != "Windows":
                    loop.remove_signal_handler(signal.SIGINT)

            # Exit session if killed externally
            if session_status == "cancelled":
                break
    except Exception:
        session_status = "error"
        raise
    finally:
        # End observability session
        if observability:
            await observability.end_session(session_status)

        # Save conversation on exit (only if there's history)
        if session.history:
            saved_path = conversation_store.save(
                model=model,
                system_prompt=session.system_prompt,
                history=session.history,
                input_tokens=session.total_input_tokens,
                output_tokens=session.total_output_tokens,
                started=session_started,
                conversation_id=conversation_id,
            )
            console.print(f"\n[dim]Goodbye! Conversation saved to {saved_path}[/dim]")
        else:
            console.print(_markup("\nGoodbye!", THEME.muted))


async def run_single(
    agent: Agent,
    prompt: str,
    observability: ObservabilityProcessor | None = None,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run a single prompt and exit."""
    # Start observability session if enabled
    if observability:
        await observability.start_session()

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        # Wrap event stream with observability if enabled
        events = agent.run_turn(prompt)
        if observability:
            events = observability.wrap_turn(events, prompt)

        status_ctx = None
        start = monotonic()
        if interactive_tty:
            status_ctx = console.status(
                "⠋ working (0s)",
                spinner="dots",
                spinner_style=THEME.accent,
            )
            status_ctx.__enter__()
        saw_model_output = False

        async for event in events:
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if observability:
                        observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                break
            handle_event(event)
        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        if observability:
            await observability.end_session(session_status)


async def run_single_with_output(
    agent: Agent,
    prompt: str,
    output_path: str,
    observability: ObservabilityProcessor | None = None,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> bool:
    """Run a single prompt and write final response to file.

    Returns True if successful, False if output file already exists.
    """
    output_file = Path(output_path).expanduser().resolve()

    # Check if output file already exists before running
    if output_file.exists():
        console.print(_markup(f"Output file already exists: {output_file}", THEME.error))
        console.print(
            _markup(
                "Use a different path or delete the existing file first.",
                THEME.muted,
            )
        )
        return False

    # Start observability session if enabled
    if observability:
        await observability.start_session()

    collected_text: list[str] = []
    cancelled = False

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        # Wrap event stream with observability if enabled
        events = agent.run_turn(prompt)
        if observability:
            events = observability.wrap_turn(events, prompt)

        status_ctx = None
        start = monotonic()
        if interactive_tty:
            status_ctx = console.status(
                "⠋ working (0s)",
                spinner="dots",
                spinner_style=THEME.accent,
            )
            status_ctx.__enter__()
        saw_model_output = False

        async for event in events:
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if observability:
                        observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                cancelled = True
                break
            handle_event(event)
            # Collect text for output file
            if event.type == "text" and event.content:
                collected_text.append(event.content)
        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        if observability:
            await observability.end_session(session_status)

    if cancelled:
        return False

    # Write collected text to output file
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("".join(collected_text), encoding="utf-8")
        console.print(f"\n{_markup(f'Output written to: {output_file}', THEME.success)}")
        return True
    except Exception as exc:
        console.print(f"\n{_markup(f'Failed to write output: {exc}', THEME.error)}")
        return False


@app.command()
def main(
    prompt_arg: Annotated[
        Optional[str],
        typer.Argument(
            metavar="PROMPT",
            help="Single prompt to run (omit for interactive mode)",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model to use"),
    ] = os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="API base URL for OpenAI-compatible endpoints"),
    ] = os.getenv("OPENAI_BASE_URL"),
    reasoning_effort: Annotated[
        Optional[str],
        typer.Option("--reasoning-effort", help="Reasoning effort: low, medium, high"),
    ] = os.getenv("RHO_AGENT_REASONING_EFFORT"),
    system_prompt_file: Annotated[
        Optional[str],
        typer.Option(
            "--system-prompt",
            "-s",
            help="Markdown system prompt file with YAML frontmatter",
        ),
    ] = None,
    prompt: Annotated[
        Optional[str],
        typer.Option(
            "--prompt",
            "-p",
            help="High-level prompt text for one-shot mode (will use default system prompt unless specified)",
        ),
    ] = None,
    var: Annotated[
        Optional[list[str]],
        typer.Option("--var", help="Prompt variable (key=value, repeatable)"),
    ] = None,
    vars_file: Annotated[
        Optional[str],
        typer.Option("--vars-file", help="YAML file with prompt variables"),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Write final response to file"),
    ] = None,
    working_dir: Annotated[
        Optional[str],
        typer.Option(
            "--working-dir", "-w", help="Working directory for shell commands"
        ),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", "-y", help="Auto-approve all tool calls"),
    ] = False,
    resume: Annotated[
        Optional[str],
        typer.Option(
            "--resume",
            "-r",
            help="Resume a conversation (use 'latest' or a conversation ID)",
        ),
    ] = None,
    list_conversations: Annotated[
        bool,
        typer.Option("--list", "-l", help="List saved conversations and exit"),
    ] = False,
    preview_lines: Annotated[
        int,
        typer.Option(
            "--preview-lines", help="Lines of tool output to show (0 to disable)"
        ),
    ] = int(os.getenv("RHO_AGENT_PREVIEW_LINES", "6")),
    profile: Annotated[
        Optional[str],
        typer.Option(
            "--profile",
            help="Capability profile: 'readonly' (default), 'developer', 'eval', or path to YAML",
        ),
    ] = os.getenv("RHO_AGENT_PROFILE"),
    shell_mode: Annotated[
        Optional[str],
        typer.Option(
            "--shell-mode",
            help="Override shell mode: 'restricted' or 'unrestricted'",
        ),
    ] = None,
    team_id: Annotated[
        Optional[str],
        typer.Option(
            "--team-id",
            help="Team ID for observability (enables telemetry)",
        ),
    ] = os.getenv("RHO_AGENT_TEAM_ID"),
    project_id: Annotated[
        Optional[str],
        typer.Option(
            "--project-id",
            help="Project ID for observability (enables telemetry)",
        ),
    ] = os.getenv("RHO_AGENT_PROJECT_ID"),
    observability_config: Annotated[
        Optional[str],
        typer.Option(
            "--observability-config",
            help="Path to observability config file",
        ),
    ] = os.getenv("RHO_AGENT_OBSERVABILITY_CONFIG"),
) -> None:
    """rho-agent: An agent harness and CLI with readonly and developer modes."""
    # Set preview lines for tool output display
    global TOOL_PREVIEW_LINES
    TOOL_PREVIEW_LINES = preview_lines

    # Initialize conversation store
    conversation_store = ConversationStore(CONFIG_DIR)

    # Handle --list: show saved conversations and exit
    if list_conversations:
        conversations = conversation_store.list_conversations()
        if not conversations:
            console.print(_markup("No saved conversations found.", THEME.muted))
            raise typer.Exit(0)

        console.print("[bold]Saved conversations:[/bold]\n")
        for conv in conversations:
            # Parse the started time for display
            try:
                started_dt = datetime.fromisoformat(conv.started)
                time_str = started_dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                time_str = conv.id
            console.print(
                f"{_markup(conv.id, THEME.accent)}  {time_str}  "
                f"{_markup(conv.model, THEME.muted)}"
            )
            console.print(f"  {conv.display_preview}")
            console.print()
        raise typer.Exit(0)

    # Handle --resume: load a previous conversation
    conversation_id: str | None = None
    resumed_conversation = None
    if resume:
        if resume.lower() == "latest":
            conversation_id = conversation_store.get_latest_id()
            if not conversation_id:
                console.print(_markup("No saved conversations to resume.", THEME.error))
                raise typer.Exit(1)
        else:
            conversation_id = resume

        resumed_conversation = conversation_store.load(conversation_id)
        if not resumed_conversation:
            console.print(
                _markup(f"Conversation not found: {conversation_id}", THEME.error)
            )
            console.print(_markup("Use --list to see saved conversations.", THEME.muted))
            raise typer.Exit(1)

    # Resolve working directory
    resolved_working_dir = (
        str(Path(working_dir).expanduser().resolve()) if working_dir else os.getcwd()
    )

    # Track when session started
    session_started = datetime.now()

    # Generate session ID and set up signal manager for ps/kill support
    session_id = str(uuid.uuid4())
    signal_manager = SignalManager()

    # Load capability profile (needed for prompt rendering)
    if profile:
        try:
            capability_profile = load_profile(profile)
        except (ValueError, FileNotFoundError) as e:
            console.print(_markup(str(InvalidProfileError(str(e))), THEME.error))
            raise typer.Exit(1) from e
    else:
        capability_profile = CapabilityProfile.readonly()
    mode_name = capability_profile.name

    # Apply command-line overrides to profile
    if shell_mode:
        try:
            capability_profile.shell = ShellMode(shell_mode)
        except ValueError:
            console.print(
                _markup(
                    str(
                        InvalidModeError(
                            "shell mode", shell_mode, "restricted, unrestricted"
                        )
                    ),
                    THEME.error,
                )
            )
            raise typer.Exit(1)

    # Build system prompt and initial prompt (skip if resuming)
    initial_prompt: str | None = None
    system_prompt: str = ""

    if resumed_conversation:
        # System prompt will be loaded from the conversation
        pass
    elif system_prompt_file:
        # --system-prompt loads markdown file as system prompt
        template_path = system_prompt_file
        try:
            loaded_prompt = load_prompt(template_path)
        except FileNotFoundError as exc:
            console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
            raise typer.Exit(1) from exc
        except ValueError as exc:
            console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
            raise typer.Exit(1) from exc

        # Collect variables from --vars-file and --var flags
        prompt_vars: dict[str, str] = {}

        if vars_file:
            vars_path = Path(vars_file).expanduser().resolve()
            if not vars_path.exists():
                console.print(
                    _markup(
                        str(PromptLoadError(f"Vars file not found: {vars_path}")),
                        THEME.error,
                    )
                )
                raise typer.Exit(1)
            try:
                with open(vars_path, encoding="utf-8") as f:
                    file_vars = yaml.safe_load(f)
                if isinstance(file_vars, dict):
                    prompt_vars.update({k: str(v) for k, v in file_vars.items()})
            except Exception as exc:
                console.print(
                    _markup(str(PromptLoadError(f"Failed to load vars file: {exc}")), THEME.error)
                )
                raise typer.Exit(1) from exc

        # --var flags override vars file
        if var:
            try:
                prompt_vars.update(parse_vars(var))
            except ValueError as exc:
                console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
                raise typer.Exit(1) from exc

        # Prepare the prompt
        try:
            system_prompt, initial_prompt = prepare_prompt(loaded_prompt, prompt_vars)
        except ValueError as exc:
            console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
            raise typer.Exit(1) from exc
    else:
        # Prefer user default prompt, otherwise package default prompt.
        default_prompt_path = (
            DEFAULT_PROMPT_FILE if DEFAULT_PROMPT_FILE.exists() else BUILTIN_PROMPT_FILE
        )
        try:
            loaded_prompt = load_prompt(default_prompt_path)
        except (FileNotFoundError, ValueError) as exc:
            console.print(
                _markup(
                    str(
                        PromptLoadError(
                            f"Could not load default prompt at {default_prompt_path}: {exc}"
                        )
                    ),
                    THEME.error,
                )
            )
            raise typer.Exit(1) from exc

        # Provide profile-aware variables
        prompt_vars = {
            "platform": platform.system(),
            "home_dir": str(Path.home()),
            "working_dir": resolved_working_dir,
            "profile_name": capability_profile.name,
            "shell_mode": capability_profile.shell.value,
            "file_write_mode": capability_profile.file_write.value,
            "database_mode": capability_profile.database.value,
        }
        try:
            system_prompt, initial_prompt = prepare_prompt(loaded_prompt, prompt_vars)
        except ValueError as exc:
            console.print(
                _markup(f"Prompt error: {PromptLoadError(str(exc))}", THEME.error)
            )
            raise typer.Exit(1) from exc

    # Set up components - use resumed conversation if available
    if resumed_conversation:
        session = Session(system_prompt=resumed_conversation.system_prompt)
        session.history = resumed_conversation.history.copy()
        session.total_input_tokens = resumed_conversation.input_tokens
        session.total_output_tokens = resumed_conversation.output_tokens
        # Use the model from resumed conversation unless explicitly overridden
        effective_model = (
            model
            if model != os.getenv("OPENAI_MODEL", "gpt-5-mini")
            else resumed_conversation.model
        )
        # Parse the original start time
        session_started = datetime.fromisoformat(resumed_conversation.started)
        console.print(
            _markup(f"Resuming conversation: {conversation_id}", THEME.success)
        )
    else:
        session = Session(system_prompt=system_prompt)
        effective_model = model

    if not base_url and not os.getenv("OPENAI_API_KEY"):
        console.print(_markup(str(MissingApiKeyError()), THEME.error))
        raise typer.Exit(1)


    registry = create_registry(working_dir=resolved_working_dir, profile=capability_profile)
    client = ModelClient(model=effective_model, base_url=base_url, reasoning_effort=reasoning_effort)
    approval_handler = ApprovalHandler(auto_approve=auto_approve)

    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=approval_handler.check_approval,
        cancel_check=lambda: signal_manager.is_cancelled(session_id),
    )

    # Create observability processor if team_id and project_id provided
    observability_processor: ObservabilityProcessor | None = None
    if team_id and project_id:
        try:
            obs_config = ObservabilityConfig.load(
                config_path=observability_config,
                team_id=team_id,
                project_id=project_id,
            )
            if obs_config.enabled and obs_config.tenant:
                from .observability.context import TelemetryContext
                context = TelemetryContext.from_config(
                    obs_config,
                    model=effective_model,
                    profile=capability_profile.name if hasattr(capability_profile, 'name') else str(capability_profile.shell.value),
                )
                # Use same session ID as signal manager for consistency
                context.session_id = session_id
                observability_processor = ObservabilityProcessor(obs_config, context)
                console.print(
                    f"[dim]Telemetry: {obs_config.tenant.team_id}/{obs_config.tenant.project_id}[/dim]"
                )
        except Exception as e:
            console.print(
                _markup(
                    f"Warning: Failed to initialize observability: {e}",
                    THEME.warning,
                )
            )

    # Determine the prompt to run
    # Prompt precedence:
    # 1) --prompt (explicit text)
    # 2) positional prompt argument
    # 3) template initial_prompt
    run_prompt = prompt if prompt else (prompt_arg if prompt_arg else initial_prompt)

    if not run_prompt and not _is_interactive_terminal():
        console.print(
            _markup(
                "Non-interactive terminal detected. Provide a prompt argument, or run in a TTY for interactive mode.",
                THEME.warning,
            )
        )
        raise typer.Exit(1)

    # Register this agent session for ps/kill support
    agent_info = AgentInfo(
        session_id=session_id,
        pid=os.getpid(),
        model=effective_model,
        instruction_preview=(run_prompt or "interactive session")[:100],
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    signal_manager.register(agent_info)

    try:
        if run_prompt:
            console.print(_markup(f"Mode: {mode_name}", THEME.accent))
            # Single prompt mode (from --prompt text, positional arg, or template initial_prompt)
            if output:
                # Capture output to file
                asyncio.run(run_single_with_output(
                    agent, run_prompt, output, observability_processor,
                    signal_manager=signal_manager, session_id=session_id,
                ))
            else:
                asyncio.run(run_single(
                    agent, run_prompt, observability_processor,
                    signal_manager=signal_manager, session_id=session_id,
                ))
        else:
            asyncio.run(
                run_interactive(
                    agent,
                    approval_handler,
                    session,
                    effective_model,
                    mode_name,
                    registry,
                    resolved_working_dir,
                    conversation_store,
                    session_started,
                    conversation_id,
                    observability_processor,
                    signal_manager=signal_manager,
                    session_id=session_id,
                )
            )
    finally:
        signal_manager.deregister(session_id)


@app.command()
def dashboard(
    db_path: Annotated[
        Optional[str],
        typer.Option("--db", help="Path to telemetry database"),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to run dashboard on"),
    ] = 8501,
) -> None:
    """Launch the observability dashboard."""
    import subprocess
    import sys

    # Set database path in environment
    resolved_db = db_path or str(DEFAULT_TELEMETRY_DB)
    env = os.environ.copy()
    env["RHO_AGENT_TELEMETRY_DB"] = resolved_db

    # Get path to dashboard app
    dashboard_path = Path(__file__).parent / "observability" / "dashboard" / "app.py"

    if not dashboard_path.exists():
        console.print(_markup(f"Dashboard app not found at {dashboard_path}", THEME.error))
        raise typer.Exit(1)

    console.print(_markup(f"Starting dashboard on port {port}...", THEME.success))
    console.print(_markup(f"Database: {resolved_db}", THEME.muted))
    console.print(_markup(f"Open http://localhost:{port} in your browser", THEME.muted))

    try:
        subprocess.run(
            [
                sys.executable, "-m", "streamlit", "run",
                str(dashboard_path),
                "--server.port", str(port),
                "--server.headless", "true",
            ],
            env=env,
            check=True,
        )
    except KeyboardInterrupt:
        console.print(_markup("\nDashboard stopped", THEME.muted))
    except subprocess.CalledProcessError as e:
        console.print(_markup(f"Dashboard failed to start: {e}", THEME.error))
        raise typer.Exit(1) from e


@app.command()
def ps(
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Remove stale entries from crashed agents"),
    ] = False,
) -> None:
    """List running rho-agent sessions."""
    sm = SignalManager()

    if cleanup:
        cleaned = sm.cleanup_stale()
        if cleaned:
            for sid in cleaned:
                console.print(f"[dim]Cleaned: {sid[:8]}[/dim]")
            console.print(_markup(f"Removed {len(cleaned)} stale entries", THEME.success))
        else:
            console.print("[dim]No stale entries found[/dim]")

    agents = sm.list_running()
    if not agents:
        console.print("[dim]No running agents[/dim]")
        raise typer.Exit(0)

    now = datetime.now(timezone.utc)
    for info in agents:
        short_id = info.session_id[:8]
        try:
            started = datetime.fromisoformat(info.started_at)
            elapsed = now - started
            secs = int(elapsed.total_seconds())
            if secs < 60:
                duration = f"{secs}s"
            elif secs < 3600:
                duration = f"{secs // 60}m{secs % 60}s"
            else:
                duration = f"{secs // 3600}h{(secs % 3600) // 60}m"
        except ValueError:
            duration = "?"
        preview = info.instruction_preview[:50]
        if len(info.instruction_preview) > 50:
            preview += "..."
        console.print(
            f"  {_markup(short_id, THEME.accent)}  {_markup('running', THEME.success)}  "
            f"{_markup(f'{info.model:<14}', THEME.muted)}  {duration:>6}  {preview}"
        )


@app.command()
def kill(
    prefix: Annotated[
        Optional[str],
        typer.Argument(help="Session ID prefix to kill"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Kill all running agents"),
    ] = False,
) -> None:
    """Kill running rho-agent sessions by session ID prefix."""
    sm = SignalManager()

    if all:
        cancelled = sm.cancel_all()
        if cancelled:
            for sid in cancelled:
                console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
            console.print(
                _markup(
                    f"Sent cancel signal to {len(cancelled)} agents", THEME.success
                )
            )
        else:
            console.print("[dim]No running agents to kill[/dim]")
        return

    if not prefix:
        console.print(
            _markup("Provide a session ID prefix, or use --all", THEME.error)
        )
        raise typer.Exit(1)

    cancelled = sm.cancel_by_prefix(prefix)
    if cancelled:
        for sid in cancelled:
            console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
    else:
        console.print(
            _markup(f"No running agents matching prefix '{prefix}'", THEME.error)
        )
        raise typer.Exit(1)


def cli() -> None:
    """CLI entrypoint with `main` as the default command."""
    args = sys.argv[1:]
    subcommands = {"main", "dashboard", "ps", "kill"}

    if not args or args[0] not in subcommands:
        args = ["main", *args]

    app(args=args, prog_name="rho-agent")


if __name__ == "__main__":
    cli()
