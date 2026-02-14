"""CLI entry point for rho-agent."""

# ---------------------------------------------------------------------------
# User-facing CLI error types
# ---------------------------------------------------------------------------


class CliUsageError(ValueError):
    """Base class for user-facing CLI configuration and usage errors."""


class MissingApiKeyError(CliUsageError):
    """Raised when API credentials are required but missing."""

    def __init__(self) -> None:
        super().__init__(
            "Missing API key. Set OPENAI_API_KEY, or pass --base-url to an endpoint "
            "that does not require OpenAI credentials."
        )


class InvalidProfileError(CliUsageError):
    """Raised when a capability profile cannot be loaded."""

    def __init__(self, details: str) -> None:
        super().__init__(
            f"Invalid profile: {details}. Use --profile readonly|developer|eval or a "
            "valid YAML profile path."
        )


class InvalidModeError(CliUsageError):
    """Raised when mode option values are invalid."""

    def __init__(self, option: str, value: str, allowed: str) -> None:
        super().__init__(f"Invalid {option} '{value}'. Allowed values: {allowed}.")


class PromptLoadError(CliUsageError):
    """Raised when prompt files or prompt variables cannot be loaded."""

    def __init__(self, details: str) -> None:
        super().__init__(f"Prompt configuration error: {details}")

import asyncio
import importlib.metadata
import json
import os
import platform
import re
import shlex
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
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
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from .capabilities import CapabilityProfile, ShellMode
from .capabilities.factory import load_profile
from .command_center.services.control_plane import ControlPlane
from .command_center.services.local_signal_transport import LocalSignalTransport
from .core.agent import AgentEvent
from .core.conversations import ConversationStore
from .core.session import Session
from .observability.config import DEFAULT_TELEMETRY_DB
from .observability.storage.sqlite import TelemetryStorage
from .prompts import load_prompt, parse_vars, prepare_prompt
from .runtime import (
    ObservabilityInitializationError,
    RuntimeOptions,
    close_runtime,
    create_runtime,
    reconfigure_runtime,
    start_runtime,
)
from .runtime.types import AgentRuntime
from .signals import AgentInfo, SignalManager
from .ui.theme import THEME

# Config directory for rho-agent data
CONFIG_DIR = Path.home() / ".config" / "rho-agent"
HISTORY_FILE = CONFIG_DIR / "history"
DEFAULT_PROMPT_FILE = CONFIG_DIR / "default.md"

# Built-in default prompt (ships with package)
BUILTIN_PROMPT_FILE = Path(__file__).parent / "prompts" / "default.md"

# Tool output preview lines (0 to disable)
TOOL_PREVIEW_LINES = int(os.getenv("RHO_AGENT_PREVIEW_LINES", "6"))

# Render assistant output as markdown in interactive TTY sessions
RENDER_MARKDOWN = os.getenv("RHO_AGENT_RENDER_MARKDOWN", "1").lower() not in (
    "0",
    "false",
    "no",
)

# Rich console for all output
console = Console()

MARKDOWN_THEME = Theme(
    {
        "markdown": THEME.primary,
        "markdown.paragraph": THEME.primary,
        "markdown.text": THEME.primary,
        "markdown.item": THEME.primary,
        "markdown.item.bullet": THEME.primary,
        "markdown.code": THEME.primary,
        "markdown.code_block": THEME.primary,
        "markdown.block_quote": THEME.muted,
        "markdown.h1": f"bold {THEME.primary}",
        "markdown.h2": f"bold {THEME.primary}",
        "markdown.h3": f"bold {THEME.primary}",
        "markdown.h4": f"bold {THEME.primary}",
        "markdown.h5": f"bold {THEME.primary}",
        "markdown.h6": f"bold {THEME.primary}",
        "markdown.link": THEME.accent,
        "markdown.em": f"italic {THEME.primary}",
        "markdown.strong": f"bold {THEME.primary}",
    }
)
# Typer app
app = typer.Typer(
    name="rho-agent",
    help="An agent harness and CLI with readonly and developer modes.",
    epilog=(
        "Examples:\n"
        "  rho-agent\n"
        '  rho-agent "What errors are in app.log?"\n'
        '  rho-agent --prompt "Investigate why CI is failing"\n'
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


def _format_observability_init_error(exc: ObservabilityInitializationError) -> str:
    cause = exc.__cause__
    if isinstance(cause, FileNotFoundError):
        return (
            f"Observability initialization failed: {cause}. "
            "Fix --observability-config to point to an existing YAML file and retry."
        )

    if isinstance(cause, yaml.YAMLError):
        config_location = exc.config_path or "the observability config file"
        return (
            f"Observability initialization failed: invalid YAML in {config_location}: "
            f"{cause}. Fix the YAML syntax and retry."
        )

    if isinstance(cause, ValueError) and "tenant" in str(cause).lower():
        return (
            "Observability initialization failed: missing tenant information. "
            "Set both --team-id and --project-id, or define "
            "observability.tenant.team_id and observability.tenant.project_id in "
            "the observability config."
        )

    guidance = []
    if exc.config_path:
        guidance.append("verify --observability-config points to a valid YAML file")
    if exc.team_id and not exc.project_id:
        guidance.append("provide --project-id")
    if exc.project_id and not exc.team_id:
        guidance.append("provide --team-id")
    if not guidance:
        guidance.append("set both --team-id and --project-id when observability is enabled")

    return f"Observability initialization failed: {cause or exc}. To fix: {'; '.join(guidance)}."


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
        self.total_input_tokens = usage.get("total_input_tokens", self.total_input_tokens)
        self.total_output_tokens = usage.get("total_output_tokens", self.total_output_tokens)

    def render(self) -> str:
        return (
            f"context:{self.context_size} "
            f"in:{self.total_input_tokens} "
            f"out:{self.total_output_tokens}"
        )


def _sync_token_status_from_session(token_status: TokenStatus, session: Session) -> None:
    """Sync UI token status from current session counters."""
    token_status.context_size = session.last_input_tokens
    token_status.total_input_tokens = session.total_input_tokens
    token_status.total_output_tokens = session.total_output_tokens


def _format_token_count(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def _format_elapsed(started_at: datetime, ended_at: datetime | None = None) -> str:
    end = ended_at or datetime.now(timezone.utc)
    elapsed = end - started_at
    secs = int(elapsed.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


async def _wait_while_paused(signal_manager: SignalManager, session_id: str) -> bool:
    """Block at a turn boundary while paused; return False if externally cancelled."""
    announced = False
    while signal_manager.is_paused(session_id):
        if signal_manager.is_cancelled(session_id):
            return False
        if not announced:
            console.print(_markup("Paused by rho-agent monitor; waiting for resume...", THEME.warning))
            announced = True
        await asyncio.sleep(0.5)
    if announced:
        console.print(_markup("Resumed by rho-agent monitor", THEME.success))
    return True


# Commands the user can type during the session
COMMANDS = [
    "/approve",
    "/compact",
    "/mode",
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

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
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
                        completion_text = str(path.parent / name) if "/" in path_text else name

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

        # bash tool
        if tool_name == "bash":
            timed_out = metadata.get("timed_out", False)
            duration = metadata.get("duration_seconds")
            exit_code = metadata.get("exit_code")
            if timed_out:
                if duration is not None:
                    return f"Command timed out ({duration}s)"
                return "Command timed out"
            if exit_code == 0:
                if duration is not None:
                    return f"Command succeeded ({duration}s)"
                return "Command succeeded"
            if exit_code is not None:
                if duration is not None:
                    return f"Command failed (exit {exit_code}, {duration}s)"
                return f"Command failed (exit {exit_code})"

        # delegate tool
        if tool_name == "delegate":
            child_status = metadata.get("child_status")
            duration = metadata.get("duration_seconds")
            if isinstance(child_status, str) and duration is not None:
                return f"Sub-agent {child_status} ({duration}s)"
            if isinstance(child_status, str):
                return f"Sub-agent {child_status}"
            if duration is not None:
                return f"Sub-agent finished ({duration}s)"
            return "Sub-agent finished"

    # Fallback: count lines in result
    if result:
        lines = result.count("\n") + 1
        if lines > 1:
            return f"{lines} lines"

    return None


def _format_tool_preview(
    result: str | None,
    tool_name: str | None = None,
    max_lines: int | None = None,
) -> str | None:
    """Get first N lines of tool output as a preview."""
    if max_lines is None:
        max_lines = TOOL_PREVIEW_LINES

    if not result or max_lines <= 0:
        return None

    display_result = result

    # Bash returns a JSON wrapper; show command output body in interactive preview.
    if tool_name == "bash":
        try:
            payload = json.loads(result)
            if isinstance(payload, dict):
                output = payload.get("output")
                if isinstance(output, str):
                    display_result = output
        except json.JSONDecodeError:
            pass

    lines = display_result.split("\n")
    if len(lines) <= max_lines:
        return display_result

    preview_lines = lines[:max_lines]
    remaining = len(lines) - max_lines
    preview_lines.append(f"... ({remaining} more lines)")
    return "\n".join(preview_lines)


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
    runtime: AgentRuntime,
    profile_name_or_path: str,
    *,
    working_dir: str,
) -> CapabilityProfile:
    """Switch runtime capabilities to a new profile for the active session."""
    try:
        capability_profile = reconfigure_runtime(
            runtime,
            profile=profile_name_or_path,
            working_dir=working_dir,
        )
    except (ValueError, FileNotFoundError) as e:
        raise InvalidProfileError(str(e)) from e

    return capability_profile


async def run_interactive(
    runtime: AgentRuntime,
    approval_handler: ApprovalHandler,
    mode_name: str,
    working_dir: str,
    conversation_store: ConversationStore,
    session_started: datetime,
    conversation_id: str | None = None,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run an interactive REPL session."""
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Start observability session if enabled
    await start_runtime(runtime)

    token_status = TokenStatus(
        input_tokens=runtime.session.total_input_tokens,
        output_tokens=runtime.session.total_output_tokens,
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
        if runtime.observability
        else _markup("telemetry off", THEME.muted)
    )
    version = _get_version()
    console.print(
        Panel(
            f"[bold]{_markup('ρ rho-agent', THEME.primary)}[/bold] v{version}\n"
            f"Mode: {_markup(mode_name, THEME.accent)}\n"
            f"Model: {_markup(runtime.model, THEME.accent)} {obs_status}\n"
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
        has_write = "write" in runtime.registry

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

            runtime.registry.register(WriteHandler(create_only=True, requires_approval=True))
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
            runtime.registry.unregister("write")
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
            console.print(_markup(f"Conversation not found: {selected_id}", THEME.error))
            return

        runtime.session.system_prompt = resumed.system_prompt
        runtime.session.history = resumed.history.copy()
        runtime.session.total_input_tokens = resumed.input_tokens
        runtime.session.total_output_tokens = resumed.output_tokens
        token_status.context_size = 0
        token_status.total_input_tokens = resumed.input_tokens
        token_status.total_output_tokens = resumed.output_tokens
        conversation_id = resumed.id
        try:
            session_started = datetime.fromisoformat(resumed.started)
        except ValueError:
            session_started = datetime.now()

        console.print(_markup(f"Resumed conversation: {resumed.id}", THEME.success))
        console.print(
            _markup(
                f"Messages: {len(resumed.history)}  "
                f"tokens in/out: {resumed.input_tokens}/{resumed.output_tokens}",
                THEME.muted,
            )
        )

    def handle_mode_switch(cmd: str) -> None:
        nonlocal mode_name

        parts = cmd.split(maxsplit=1)
        if len(parts) == 1 or parts[1].strip().lower() in ("status", "current"):
            console.print(_markup(f"Current mode: {mode_name}", THEME.accent))
            console.print(
                _markup(
                    "Usage: /mode <readonly|developer|eval|profile-path>",
                    THEME.muted,
                )
            )
            return

        target = parts[1].strip()
        try:
            capability_profile = switch_runtime_profile(
                runtime,
                target,
                working_dir=working_dir,
            )
        except InvalidProfileError as e:
            console.print(_markup(str(e), THEME.error))
            return

        mode_name = capability_profile.name
        console.print(_markup(f"Switched mode to {mode_name}", THEME.success))
        console.print(
            _markup(
                (
                    "shell="
                    f"{capability_profile.shell.value}, "
                    "file_write="
                    f"{capability_profile.file_write.value}, "
                    "database="
                    f"{capability_profile.database.value}"
                ),
                THEME.muted,
            )
        )

    async def execute_turn(user_input: str) -> None:
        nonlocal session_status

        loop = asyncio.get_event_loop()
        response_chunks: list[str] = []

        def on_cancel():
            console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
            runtime.agent.request_cancel()

        # Register signal handler for this turn (Unix only)
        if platform.system() != "Windows":
            loop.add_signal_handler(signal.SIGINT, on_cancel)

        try:
            events = runtime.agent.run_turn(user_input)
            if runtime.observability:
                events = runtime.observability.wrap_turn(events, user_input)

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
            pending_text_chunks: list[str] = []

            async for event in events:
                if status_ctx and not saw_model_output:
                    elapsed = int(monotonic() - start)
                    status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
                if event.type == "text" and event.content:
                    response_chunks.append(event.content)
                if event.type in ("text", "tool_start", "error", "cancelled"):
                    saw_model_output = True
                    if status_ctx:
                        status_ctx.__exit__(None, None, None)
                        status_ctx = None
                if event.type == "cancelled":
                    _sync_token_status_from_session(token_status, runtime.session)
                    if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                        session_status = "cancelled"
                        if runtime.observability:
                            runtime.observability.context.metadata["cancel_source"] = "kill_command"
                        console.print(_markup("Killed by rho-agent kill", THEME.warning))
                    else:
                        console.print(_markup("Turn cancelled", THEME.muted))
                    break
                if event.type == "error":
                    _sync_token_status_from_session(token_status, runtime.session)
                handle_event(
                    event,
                    show_turn_usage=False,
                    token_status=token_status,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
            if status_ctx:
                status_ctx.__exit__(None, None, None)
            if signal_manager and session_id and response_chunks:
                signal_manager.record_response(session_id, "".join(response_chunks))
        finally:
            if platform.system() != "Windows":
                loop.remove_signal_handler(signal.SIGINT)

    try:
        while True:
            if signal_manager and session_id:
                if not await _wait_while_paused(signal_manager, session_id):
                    session_status = "cancelled"
                    if runtime.observability:
                        runtime.observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                    break

                directives = signal_manager.consume_directives(session_id)
                for directive in directives:
                    console.print(_markup(f"Directive received: {directive}", THEME.secondary))
                    await execute_turn(directive)
                    if session_status == "cancelled":
                        break
                if session_status == "cancelled":
                    break

                if signal_manager.has_export_request(session_id):
                    from .context_export import write_context_file

                    write_context_file(
                        signal_manager.context_path(session_id),
                        runtime.session.get_messages(),
                    )
                    signal_manager.clear_export_request(session_id)

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
                if user_input.startswith("/mode"):
                    handle_mode_switch(user_input)
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
                    result = await runtime.agent.compact(
                        custom_instructions=instructions, trigger="manual"
                    )
                    handle_event(
                        AgentEvent(
                            type="compact_end",
                            content=f"Compacted: {result.tokens_before} → {result.tokens_after} tokens",
                        )
                    )
                continue

            await execute_turn(user_input)
            if session_status == "cancelled":
                break
    except Exception:
        session_status = "error"
        raise
    finally:
        # End observability session
        await close_runtime(runtime, session_status)

        # Save conversation on exit (only if there's history)
        if runtime.session.history:
            saved_path = conversation_store.save(
                model=runtime.model,
                system_prompt=runtime.session.system_prompt,
                history=runtime.session.history,
                input_tokens=runtime.session.total_input_tokens,
                output_tokens=runtime.session.total_output_tokens,
                started=session_started,
                conversation_id=conversation_id,
            )
            console.print(f"\n[dim]Goodbye! Conversation saved to {saved_path}[/dim]")
        else:
            console.print(_markup("\nGoodbye!", THEME.muted))


async def run_single(
    runtime: AgentRuntime,
    prompt: str,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run a single prompt and exit."""
    # Start observability session if enabled
    await start_runtime(runtime)

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        runtime.agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                session_status = "cancelled"
                if runtime.observability:
                    runtime.observability.context.metadata["cancel_source"] = "kill_command"
                console.print(_markup("Killed by rho-agent kill", THEME.warning))
                return
            directives = signal_manager.consume_directives(session_id)
            if directives:
                console.print(
                    _markup(
                        "Ignoring queued directives in single-prompt mode.",
                        THEME.muted,
                    )
                )

        # Wrap event stream with observability if enabled
        events = runtime.agent.run_turn(prompt)
        if runtime.observability:
            events = runtime.observability.wrap_turn(events, prompt)

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
        pending_text_chunks: list[str] = []

        async for event in events:
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "error":
                session_status = "error"
                handle_event(
                    event,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
                break
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if runtime.observability:
                        runtime.observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                break
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )
        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        await close_runtime(runtime, session_status)


async def run_single_with_output(
    runtime: AgentRuntime,
    prompt: str,
    output_path: str,
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
    await start_runtime(runtime)

    collected_text: list[str] = []
    cancelled = False
    had_error = False

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        runtime.agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                session_status = "cancelled"
                if runtime.observability:
                    runtime.observability.context.metadata["cancel_source"] = "kill_command"
                console.print(_markup("Killed by rho-agent kill", THEME.warning))
                return False
            directives = signal_manager.consume_directives(session_id)
            if directives:
                console.print(
                    _markup(
                        "Ignoring queued directives in single-prompt mode.",
                        THEME.muted,
                    )
                )

        # Wrap event stream with observability if enabled
        events = runtime.agent.run_turn(prompt)
        if runtime.observability:
            events = runtime.observability.wrap_turn(events, prompt)

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
        pending_text_chunks: list[str] = []

        async for event in events:
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "error":
                session_status = "error"
                had_error = True
                handle_event(
                    event,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
                break
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if runtime.observability:
                        runtime.observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                cancelled = True
                break
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )
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
        await close_runtime(runtime, session_status)

    if cancelled:
        return False
    if had_error:
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
        typer.Option("--working-dir", "-w", help="Working directory for shell commands"),
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
        typer.Option("--preview-lines", help="Lines of tool output to show (0 to disable)"),
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
                f"{_markup(conv.id, THEME.accent)}  {time_str}  {_markup(conv.model, THEME.muted)}"
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
            console.print(_markup(f"Conversation not found: {conversation_id}", THEME.error))
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
                    str(InvalidModeError("shell mode", shell_mode, "restricted, unrestricted")),
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
            console.print(_markup(f"Prompt error: {PromptLoadError(str(exc))}", THEME.error))
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
        console.print(_markup(f"Resuming conversation: {conversation_id}", THEME.success))
    else:
        session = Session(system_prompt=system_prompt)
        effective_model = model

    if not base_url and not os.getenv("OPENAI_API_KEY"):
        console.print(_markup(str(MissingApiKeyError()), THEME.error))
        raise typer.Exit(1)

    approval_handler = ApprovalHandler(auto_approve=auto_approve)
    try:
        runtime = create_runtime(
            session.system_prompt,
            options=RuntimeOptions(
                model=effective_model,
                base_url=base_url,
                reasoning_effort=reasoning_effort,
                working_dir=resolved_working_dir,
                profile=capability_profile,
                auto_approve=auto_approve,
                team_id=team_id,
                project_id=project_id,
                observability_config=observability_config,
                session_id=session_id,
            ),
            session=session,
            approval_callback=approval_handler.check_approval,
            cancel_check=lambda: signal_manager.is_cancelled(session_id),
        )
    except ObservabilityInitializationError as exc:
        console.print(_markup(_format_observability_init_error(exc), THEME.error))
        raise typer.Exit(1) from exc
    if runtime.observability and runtime.observability.context:
        context = runtime.observability.context
        console.print(f"[dim]Telemetry: {context.team_id}/{context.project_id}[/dim]")

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
        model=runtime.model,
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
                asyncio.run(
                    run_single_with_output(
                        runtime,
                        run_prompt,
                        output,
                        signal_manager=signal_manager,
                        session_id=session_id,
                    )
                )
            else:
                asyncio.run(
                    run_single(
                        runtime,
                        run_prompt,
                        signal_manager=signal_manager,
                        session_id=session_id,
                    )
                )
        else:
            asyncio.run(
                run_interactive(
                    runtime,
                    approval_handler,
                    mode_name,
                    resolved_working_dir,
                    conversation_store,
                    session_started,
                    conversation_id,
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
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(dashboard_path),
                "--server.port",
                str(port),
                "--server.headless",
                "true",
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
        paused = sm.is_paused(info.session_id)
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
        state = "paused" if paused else "running"
        state_color = THEME.warning if paused else THEME.success
        console.print(
            f"  {_markup(short_id, THEME.accent)}  {_markup(state, state_color)}  "
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
            console.print(_markup(f"Sent cancel signal to {len(cancelled)} agents", THEME.success))
        else:
            console.print("[dim]No running agents to kill[/dim]")
        return

    if not prefix:
        console.print(_markup("Provide a session ID prefix, or use --all", THEME.error))
        raise typer.Exit(1)

    cancelled = sm.cancel_by_prefix(prefix)
    if cancelled:
        for sid in cancelled:
            console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
    else:
        console.print(_markup(f"No running agents matching prefix '{prefix}'", THEME.error))
        raise typer.Exit(1)


@app.command()
def monitor(
    db_path: Annotated[
        Optional[str],
        typer.Option("--db", help="Path to telemetry database"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Number of sessions to list in overview"),
    ] = 20,
    read_write: Annotated[
        bool,
        typer.Option(
            "--read-write",
            help="Open telemetry DB in read-write mode (default is read-only)",
        ),
    ] = False,
) -> None:
    """Interactive command center for telemetry and live agent controls."""
    resolved_db = db_path or str(DEFAULT_TELEMETRY_DB)
    sm = SignalManager()
    control_plane = ControlPlane(LocalSignalTransport(sm))
    try:
        storage = TelemetryStorage(resolved_db, read_only=not read_write)
    except Exception as exc:
        console.print(_markup(f"Failed to open telemetry DB: {exc}", THEME.error))
        raise typer.Exit(1) from exc
    connection_state: dict[str, Any] | None = None

    def print_help() -> None:
        console.print(_markup("Commands:", THEME.secondary))
        console.print("[dim]  overview                                running agents + active sessions[/dim]")
        console.print("[dim]  running                                 list running agents[/dim]")
        console.print(r"[dim]  sessions \[active|completed|all]        browse telemetry sessions (default: all)[/dim]")
        console.print("[dim]  show <id_or_prefix>                     session detail[/dim]")
        console.print("[dim]  watch <id_or_prefix>                    stream new tools + responses[/dim]")
        console.print("[dim]  kill <prefix|all>                       cancel running session(s)[/dim]")
        console.print("[dim]  pause <prefix|all>                      pause running session(s)[/dim]")
        console.print("[dim]  resume <prefix|all>                     resume paused session(s)[/dim]")
        console.print("[dim]  directive <prefix> <text>               inject directive into interactive run[/dim]")
        console.print(r"[dim]  connect <a> <b> \[more...] -- <task>   context-file collaboration[/dim]")
        console.print("[dim]  disconnect                              end active connect session[/dim]")
        console.print("[dim]  help                                    show this help[/dim]")
        console.print("[dim]  quit                                    exit monitor[/dim]")

    def render_running() -> None:
        agents = sm.list_running()
        if not agents:
            console.print("[dim]No running agents[/dim]")
            return
        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Session", style=THEME.accent)
        table.add_column("State")
        table.add_column("Model", style=THEME.muted)
        table.add_column("Uptime", justify="right")
        table.add_column("Preview", overflow="fold")
        now = datetime.now(timezone.utc)
        for info in agents:
            paused = sm.is_paused(info.session_id)
            state = "paused" if paused else "running"
            state_color = THEME.warning if paused else THEME.success
            try:
                started = datetime.fromisoformat(info.started_at)
            except ValueError:
                started = now
            table.add_row(
                info.session_id[:8],
                _markup(state, state_color),
                info.model,
                _format_elapsed(started, now),
                info.instruction_preview,
            )
        console.print(table)

    def render_sessions(status: str | None = None) -> None:
        sessions = storage.list_sessions(status=status, limit=limit)
        if not sessions:
            console.print("[dim]No telemetry sessions found[/dim]")
            return
        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Session", style=THEME.accent)
        table.add_column("Status")
        table.add_column("Model", style=THEME.muted)
        table.add_column("Team/Project", style=THEME.muted)
        table.add_column("Tokens", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("Turns", justify="right")
        table.add_column("Duration", justify="right")
        for s in sessions:
            total_tokens = s.total_input_tokens + s.total_output_tokens
            table.add_row(
                s.session_id[:8],
                s.status,
                s.model,
                f"{s.team_id}/{s.project_id}",
                _format_token_count(total_tokens),
                str(s.total_tool_calls),
                str(s.turn_count),
                _format_elapsed(s.started_at, s.ended_at),
            )
        console.print(table)

    def resolve_running_prefix(prefix: str) -> list[str]:
        return control_plane.resolve_running_prefix(prefix)

    def resolve_single_running(prefix: str) -> str | None:
        session_id, error = control_plane.resolve_single_running(prefix)
        if not error:
            return session_id
        color = THEME.warning if "multiple sessions" in error else THEME.error
        console.print(_markup(error, color))
        return None

    def truncate_for_directive(text: str, max_chars: int = 3000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]..."

    def wait_for_new_response(
        session_id: str,
        *,
        after_seq: int,
        timeout_seconds: int = 120,
    ) -> tuple[int, str] | None:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            latest = sm.get_last_response(session_id)
            if latest and latest[0] > after_seq:
                return latest
            sleep(0.5)
        return None

    def request_fresh_export(session_id: str) -> bool:
        sm.clear_export(session_id)
        return sm.request_export(session_id)

    def wait_for_export(
        session_id: str,
        *,
        timeout_seconds: int = 60,
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if sm.export_ready(session_id):
                return True
            sleep(0.5)
        return False

    def clear_connect_exports(session_ids: list[str]) -> None:
        for sid in session_ids:
            sm.clear_export(sid)

    def build_connect_directive(
        *,
        session_id: str,
        session_ids: list[str],
        task: str,
        prior_responses: list[tuple[str, str]],
    ) -> str:
        peer_lines = [
            f"- {peer[:8]}: {sm.context_path(peer)}" for peer in session_ids if peer != session_id
        ]
        if peer_lines:
            peer_contexts = "\n".join(peer_lines)
        else:
            peer_contexts = "- (none)"

        if prior_responses:
            prior_text = "\n\n".join(
                f"[{peer[:8]}]\n{truncate_for_directive(text)}" for peer, text in prior_responses
            )
        else:
            prior_text = "(none yet)"

        return (
            f"Task: {task}\n"
            "This is a connect collaboration turn.\n"
            "Use read/grep tools on peer context files, then respond with your analysis.\n\n"
            "Peer context files:\n"
            f"{peer_contexts}\n\n"
            "Prior agent responses in this connect session:\n"
            f"{prior_text}"
        )

    def run_connect(prefixes: list[str], task: str) -> None:
        nonlocal connection_state
        if connection_state is not None:
            console.print(
                _markup(
                    "A connect session is already active. Run 'disconnect' before starting another.",
                    THEME.warning,
                )
            )
            return

        session_ids: list[str] = []
        for prefix in prefixes:
            resolved = resolve_single_running(prefix)
            if not resolved:
                return
            if resolved in session_ids:
                console.print(_markup(f"Duplicate agent prefix '{prefix}'", THEME.warning))
                continue
            session_ids.append(resolved)

        if len(session_ids) < 2:
            console.print(_markup("connect requires at least two distinct running agents.", THEME.error))
            return

        paused = [sid[:8] for sid in session_ids if sm.is_paused(sid)]
        if paused:
            console.print(
                _markup(
                    f"Paused agents detected ({', '.join(paused)}). Resume before connect.",
                    THEME.warning,
                )
            )
            return

        seq_by_session: dict[str, int] = {}
        for sid in session_ids:
            prior = sm.get_last_response(sid)
            seq_by_session[sid] = prior[0] if prior else 0

        for sid in session_ids:
            if not request_fresh_export(sid):
                console.print(_markup(f"Failed to request export for {sid[:8]}", THEME.error))
                clear_connect_exports(session_ids)
                return

        for sid in session_ids:
            if not wait_for_export(sid):
                console.print(
                    _markup(
                        f"Timed out waiting for context export from {sid[:8]}",
                        THEME.error,
                    )
                )
                clear_connect_exports(session_ids)
                return

        responses: list[tuple[str, str]] = []

        console.print(
            _markup(
                f"Starting connect with {len(session_ids)} agents",
                THEME.success,
            )
        )

        for sid in session_ids:
            prompt = build_connect_directive(
                session_id=sid,
                session_ids=session_ids,
                task=task,
                prior_responses=responses,
            )
            if not sm.queue_directive(sid, prompt):
                console.print(_markup(f"Failed to queue directive for {sid[:8]}", THEME.error))
                clear_connect_exports(session_ids)
                return

            latest = wait_for_new_response(sid, after_seq=seq_by_session[sid])
            if not latest:
                console.print(
                    _markup(
                        f"Timed out waiting for response from {sid[:8]}",
                        THEME.warning,
                    )
                )
                clear_connect_exports(session_ids)
                return

            seq_by_session[sid], response_text = latest
            responses.append((sid, response_text))

            if not request_fresh_export(sid) or not wait_for_export(sid, timeout_seconds=15):
                console.print(
                    _markup(
                        f"Failed to refresh context export for {sid[:8]} after response.",
                        THEME.warning,
                    )
                )
                clear_connect_exports(session_ids)
                return

            console.print(_markup(f"{sid[:8]}: response captured", THEME.muted))

        connection_state = {
            "session_ids": session_ids,
            "task": task,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        console.print(
            _markup(
                f"Connect session open for {len(session_ids)} agents. Use 'disconnect' to end it.",
                THEME.success,
            )
        )

    def resolve_session_id(prefix: str) -> str | None:
        detail = storage.get_session_detail(prefix)
        if detail:
            return prefix
        recent = storage.list_sessions(limit=200)
        matches = [s.session_id for s in recent if s.session_id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def resolve_watch_session_id(prefix: str) -> str | None:
        telemetry_match = resolve_session_id(prefix)
        if telemetry_match:
            return telemetry_match
        running_match = resolve_single_running(prefix)
        if running_match:
            return running_match
        return None

    def format_tool_args_preview(arguments: object, max_chars: int = 400) -> str:
        if not isinstance(arguments, dict):
            return "{}"
        text = json.dumps(arguments, ensure_ascii=False)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "... [truncated]"

    def watch_session(prefix: str, poll_interval_seconds: float = 1.0) -> None:
        session_id = resolve_watch_session_id(prefix)
        if not session_id:
            console.print(_markup(f"Session not found for prefix '{prefix}'", THEME.error))
            return

        detail = storage.get_session_detail(session_id)
        seen_turn_ids: set[str] = set()
        seen_execution_ids: set[str] = set()
        if detail:
            for turn in detail.turns:
                turn_id = turn.get("turn_id")
                if isinstance(turn_id, str):
                    seen_turn_ids.add(turn_id)
                for tool in turn.get("tool_executions", []):
                    execution_id = tool.get("execution_id")
                    if isinstance(execution_id, str):
                        seen_execution_ids.add(execution_id)

        latest_response = sm.get_last_response(session_id)
        last_response_seq = latest_response[0] if latest_response else 0

        console.print(
            _markup(
                f"Watching {session_id[:8]} (Ctrl+C to stop)",
                THEME.success,
            )
        )
        console.print(
            _markup(
                "Streams completed tool calls from telemetry and latest assistant responses.",
                THEME.muted,
            )
        )

        waiting_for_telemetry_announced = False
        seen_running = any(a.session_id == session_id for a in sm.list_running())

        try:
            while True:
                running = any(a.session_id == session_id for a in sm.list_running())
                seen_running = seen_running or running

                detail = storage.get_session_detail(session_id)
                if detail:
                    waiting_for_telemetry_announced = False
                    for turn in detail.turns:
                        turn_id = turn.get("turn_id")
                        if not isinstance(turn_id, str):
                            continue

                        if turn_id not in seen_turn_ids:
                            seen_turn_ids.add(turn_id)
                            user_input = str(turn.get("user_input") or "").strip()
                            if user_input:
                                preview = user_input.replace("\n", " ")
                                if len(preview) > 180:
                                    preview = preview[:180] + "..."
                                console.print(
                                    _markup(
                                        f"user: {preview}",
                                        THEME.secondary,
                                    )
                                )
                            else:
                                console.print(_markup("user: (empty)", THEME.secondary))

                        for tool in turn.get("tool_executions", []):
                            execution_id = tool.get("execution_id")
                            if not isinstance(execution_id, str) or execution_id in seen_execution_ids:
                                continue
                            seen_execution_ids.add(execution_id)

                            tool_name = str(tool.get("tool_name") or "tool")
                            success = bool(tool.get("success"))
                            duration_ms = int(tool.get("duration_ms") or 0)
                            status_text = "ok" if success else "error"
                            status_color = THEME.success if success else THEME.error
                            console.print(
                                f"{_markup(f'tool: {tool_name}', THEME.tool_call)} "
                                f"{_markup(status_text, status_color)} "
                                f"{_markup(f'({duration_ms} ms)', THEME.muted)}"
                            )

                            args_preview = format_tool_args_preview(tool.get("arguments"))
                            console.print(_markup(f"args: {args_preview}", THEME.muted))

                            if not success and tool.get("error"):
                                console.print(_markup(f"error: {tool['error']}", THEME.error))

                            result_preview = _format_tool_preview(
                                str(tool.get("result") or ""),
                                tool_name,
                                max_lines=TOOL_PREVIEW_LINES,
                            )
                            if result_preview:
                                console.print(_markup(result_preview, THEME.tool_result))
                            console.print()
                elif not waiting_for_telemetry_announced:
                    console.print(
                        _markup(
                            "Waiting for telemetry rows for this session...",
                            THEME.muted,
                        )
                    )
                    waiting_for_telemetry_announced = True

                latest_response = sm.get_last_response(session_id)
                if latest_response and latest_response[0] > last_response_seq:
                    last_response_seq = latest_response[0]
                    response_text = latest_response[1].strip()
                    response_preview = response_text
                    if len(response_preview) > 500:
                        response_preview = response_preview[:500] + "... [truncated]"
                    console.print(_markup("[assistant response]", THEME.primary))
                    console.print(_markup(response_preview, THEME.primary))
                    console.print()

                if detail and detail.status != "active" and not running:
                    console.print(_markup(f"Session ended ({detail.status}).", THEME.muted))
                    return

                if not running and seen_running and detail is None:
                    console.print(_markup("Session no longer running.", THEME.muted))
                    return

                sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            console.print()
            console.print(_markup("Stopped watching.", THEME.muted))

    def show_detail(prefix: str) -> None:
        session_id = resolve_session_id(prefix)
        if not session_id:
            console.print(_markup(f"Session not found for prefix '{prefix}'", THEME.error))
            return
        detail = storage.get_session_detail(session_id)
        if not detail:
            console.print(_markup(f"Session not found: {session_id}", THEME.error))
            return

        running = next((a for a in sm.list_running() if a.session_id == detail.session_id), None)
        paused = sm.is_paused(detail.session_id)
        status = "paused" if paused and detail.status == "active" else detail.status
        if status == "active":
            status_color = THEME.success
        elif status == "paused":
            status_color = THEME.warning
        else:
            status_color = THEME.warning

        console.print(
            Panel(
                f"Session: {_markup(detail.session_id, THEME.accent)}\n"
                f"Status: {_markup(status, status_color)}\n"
                f"Model: {_markup(detail.model, THEME.accent)}\n"
                f"Team/Project: {_markup(f'{detail.team_id}/{detail.project_id}', THEME.muted)}\n"
                f"Profile: {_markup(detail.profile or '-', THEME.muted)}\n"
                f"Duration: {_markup(_format_elapsed(detail.started_at, detail.ended_at), THEME.muted)}\n"
                f"Tokens: {_markup(_format_token_count(detail.total_input_tokens + detail.total_output_tokens), THEME.muted)}\n"
                f"Tool calls: {_markup(str(detail.total_tool_calls), THEME.muted)}\n"
                f"Turns: {_markup(str(len(detail.turns)), THEME.muted)}\n"
                f"PID: {_markup(str(running.pid), THEME.muted) if running else _markup('-', THEME.muted)}",
                border_style=THEME.border,
            )
        )

        if not detail.turns:
            console.print("[dim]No turns recorded[/dim]")
            return

        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Turn")
        table.add_column("Input", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("User Input Preview", overflow="fold")
        for turn in detail.turns[-10:]:
            preview = (turn.get("user_input") or "").replace("\n", " ")
            if len(preview) > 100:
                preview = preview[:100] + "..."
            table.add_row(
                str(turn.get("turn_index", "?")),
                _format_token_count(int(turn.get("input_tokens", 0))),
                _format_token_count(int(turn.get("output_tokens", 0))),
                str(len(turn.get("tool_executions", []))),
                preview,
            )
        console.print(table)

    def render_overview() -> None:
        console.print(_markup("Running agents", THEME.secondary))
        render_running()
        console.print()
        console.print(_markup("Active sessions", THEME.secondary))
        render_sessions(status="active")
        completed_count = storage.count_sessions(status="completed")
        if completed_count:
            console.print(
                f"[dim]{completed_count} completed session{'s' if completed_count != 1 else ''}"
                " — type [bold]sessions[/bold] to browse[/dim]"
            )

    console.print(
        Panel(
            f"[bold]{_markup('rho-agent monitor', THEME.primary)}[/bold]\n"
            f"Database: {_markup(resolved_db, THEME.muted)}\n"
            f"Mode: {_markup('read-write' if read_write else 'read-only', THEME.muted)}\n"
            "Type [bold]help[/bold] for commands.",
            border_style=THEME.border,
        )
    )
    render_overview()

    while True:
        try:
            raw = input("monitor> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not raw:
            continue
        if raw in ("quit", "exit", "q"):
            break
        if raw in ("help", "h", "?"):
            print_help()
            continue
        if raw in ("overview", "o", "refresh", "r"):
            render_overview()
            continue
        if raw in ("running", "ps"):
            render_running()
            continue

        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            console.print(_markup(f"Invalid command syntax: {exc}", THEME.error))
            continue
        command = parts[0].lower()

        if command == "sessions":
            status = None
            if len(parts) > 1 and parts[1] != "all":
                status = parts[1]
            render_sessions(status=status)
            continue

        if command == "show" and len(parts) > 1:
            show_detail(parts[1])
            continue

        if command == "watch" and len(parts) > 1:
            watch_session(parts[1])
            continue

        if command in ("kill", "pause", "resume") and len(parts) > 1:
            target = parts[1]
            if command == "kill":
                outcome = control_plane.kill(target)
            elif command == "pause":
                outcome = control_plane.pause(target)
            else:
                outcome = control_plane.resume(target)

            if outcome.acted_session_ids:
                for sid in outcome.acted_session_ids:
                    console.print(_markup(f"{command}: {sid[:8]}", THEME.success))
            elif outcome.error:
                console.print(_markup(outcome.error, THEME.error))
            elif outcome.warning:
                console.print(_markup(outcome.warning, THEME.warning))
            else:
                console.print(_markup(f"No agents updated by '{command}'", THEME.warning))
            continue

        if command == "directive" and len(parts) > 2:
            target = parts[1]
            directive = " ".join(parts[2:])
            outcome = control_plane.directive(target, directive)
            if outcome.acted_session_ids:
                session_id = outcome.acted_session_ids[0]
                console.print(_markup(f"directive queued for {session_id[:8]}", THEME.success))
            elif outcome.warning:
                console.print(_markup(outcome.warning, THEME.warning))
            elif outcome.error:
                console.print(_markup(outcome.error, THEME.error))
            else:
                console.print(_markup("Failed to queue directive", THEME.error))
            continue

        if command == "connect":
            separator_index = parts.index("--") if "--" in parts else -1
            if separator_index == -1:
                console.print(
                    _markup(
                        "Usage: connect <a_prefix> <b_prefix> [more_prefixes...] -- <task>",
                        THEME.warning,
                    )
                )
                continue
            left = parts[1:separator_index]
            if len(left) < 2:
                console.print(
                    _markup(
                        "connect requires at least two agent prefixes before '--'.",
                        THEME.warning,
                    )
                )
                continue
            prefixes = left
            task = " ".join(parts[separator_index + 1 :]).strip()
            if not task:
                console.print(_markup("connect task cannot be empty.", THEME.warning))
                continue
            run_connect(prefixes, task)
            continue

        if command == "disconnect":
            if connection_state is None:
                console.print(_markup("No active connect session.", THEME.warning))
                continue

            session_ids = connection_state.get("session_ids", [])
            if not isinstance(session_ids, list):
                session_ids = []

            for sid in session_ids:
                sm.queue_directive(
                    sid,
                    "The connect session has ended. Resume your previous work.",
                )
                sm.clear_export(sid)

            connection_state = None
            console.print(_markup("Disconnected active connect session.", THEME.success))
            continue

        console.print(_markup(f"Unknown command: {raw}", THEME.warning))
        console.print("[dim]Type 'help' for command list[/dim]")


def cli() -> None:
    """CLI entrypoint with `main` as the default command."""
    # Register conductor subcommand (lazy import to avoid circular deps)
    from .conductor.cli import conduct as _conduct_fn

    app.command(name="conduct")(_conduct_fn)

    args = sys.argv[1:]
    subcommands = {"main", "dashboard", "monitor", "ps", "kill", "conduct"}

    if not args or args[0] not in subcommands:
        args = ["main", *args]

    app(args=args, prog_name="rho-agent")


if __name__ == "__main__":
    cli()
