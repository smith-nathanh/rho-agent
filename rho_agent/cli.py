"""CLI entry point for rho-agent."""

import asyncio
import os
import platform
import re
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
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
from rich.panel import Panel

from .client.model import ModelClient
from .core.agent import Agent, AgentEvent
from .core.conversations import ConversationStore
from .core.session import Session
from .prompts import load_prompt, parse_vars, prepare_prompt
from .capabilities import CapabilityProfile, ShellMode, FileWriteMode
from .capabilities.factory import ToolFactory, load_profile
from .tools.registry import ToolRegistry
from .observability.config import ObservabilityConfig, DEFAULT_TELEMETRY_DB
from .observability.processor import ObservabilityProcessor, create_processor
from .observability.context import TelemetryContext
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
    help="A read-only research assistant for inspecting logs, files, and databases.",
    add_completion=False,
)

DEFAULT_SYSTEM_PROMPT = """\
You are a research assistant that helps inspect logs, files, and databases.

## How to Use Tools
- You have tools available. Use them by calling them directly - DO NOT output JSON or tool syntax in your response.
- You can call tools MULTIPLE TIMES to complete a task. After each tool result, decide if you need more information.
- Keep investigating until you have enough information to answer the user's question completely.
- When you need to inspect files, run commands, or query databases - USE YOUR TOOLS. Do not guess or make up information.

## Constraints
- You are read-only - you cannot modify existing files or execute destructive commands.
- You CAN use the write_output tool to create new output files (summaries, reports, scripts) when asked.

## Database Tools
Database tools are only available when their connection environment variables are set:
- Oracle: Enabled when ORACLE_DSN is set (also uses ORACLE_USER, ORACLE_PASSWORD)
- SQLite: Enabled when SQLITE_DB is set (path to database file)
- Vertica: Enabled when VERTICA_HOST is set (also uses VERTICA_PORT, VERTICA_DATABASE, VERTICA_USER, VERTICA_PASSWORD)

If a user asks about a database but you don't have the corresponding tool, let them know which environment variable to set.

## Environment
- Platform: {platform}
- Home directory: {home_dir}
- Working directory: {working_dir}

When users reference paths with ~, expand them to {home_dir}.
Always use absolute paths in tool calls.
"""

# Commands the user can type during the session
COMMANDS = ["/approve", "/compact", "/help", "/clear", "exit", "quit"]

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
        console.print("[green]Auto-approve enabled for this session[/green]")

    async def check_approval(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Prompt user for approval. Returns True if approved."""
        if self.auto_approve:
            return True

        console.print("[yellow]Approve? \\[Y/n]:[/yellow] ", end="")

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


def handle_event(event: AgentEvent) -> None:
    """Handle an agent event by printing to console."""
    if event.type == "text":
        # Stream text immediately as it arrives
        print(event.content or "", end="", flush=True)

    elif event.type == "tool_start":
        # Show compact tool signature (like Claude Code)
        sig = _format_tool_signature(event.tool_name, event.tool_args)
        console.print(f"[cyan]{sig}[/cyan]")

    elif event.type == "tool_end":
        # Show a brief summary of what the tool found
        summary = _format_tool_summary(
            event.tool_name, event.tool_metadata, event.tool_result
        )
        if summary:
            console.print(f"[dim]  → {summary}[/dim]")

        # Show preview of the actual output
        preview = _format_tool_preview(event.tool_result)
        if preview:
            # Indent each line for visual grouping
            indented = "\n".join(f"    {line}" for line in preview.split("\n"))
            console.print(f"[dim]{indented}[/dim]")

    elif event.type == "tool_blocked":
        console.print("[red]Command rejected[/red]")

    elif event.type == "compact_start":
        trigger = event.content or "manual"
        if trigger == "auto":
            console.print(
                "[yellow]Context limit approaching, auto-compacting...[/yellow]"
            )
        else:
            console.print("[yellow]Compacting conversation...[/yellow]")

    elif event.type == "compact_end":
        console.print(f"[green]{event.content}[/green]")
        console.print(
            "[dim]Note: Multiple compactions can reduce accuracy. "
            "Start a new session when possible.[/dim]"
        )

    elif event.type == "turn_complete":
        # Ensure we end on a new line
        print()
        usage = event.usage or {}
        console.print(
            f"[dim][{usage.get('total_input_tokens', 0)} in, "
            f"{usage.get('total_output_tokens', 0)} out][/dim]"
        )

    elif event.type == "error":
        console.print(f"[red]Error: {event.content}[/red]")


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

    if cmd == "/help":
        console.print(
            Panel(
                "[bold]Commands:[/bold]\n"
                "  /approve             - Enable auto-approve for all tool calls\n"
                "  /compact [guidance]  - Compact conversation history\n"
                "  /help                - Show this help\n"
                "  /clear               - Clear the screen\n"
                "  exit                 - Quit the session\n"
                "\n[bold]Input:[/bold]\n"
                "  Enter               - Send message\n"
                "  Esc+Enter           - New line\n"
                "\n[bold]Conversations:[/bold]\n"
                "  rho-agent --list     - List saved conversations\n"
                "  rho-agent -r latest  - Resume most recent conversation\n"
                "  rho-agent -r <id>    - Resume specific conversation",
                title="Help",
                border_style="blue",
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
        complete_while_typing=False,
        complete_in_thread=True,
    )

    # Welcome message
    obs_status = "[green]telemetry enabled[/green]" if observability else ""
    console.print(
        Panel(
            "[bold]rho-agent[/bold] - Read-only research assistant\n"
            f"Model: [cyan]{model}[/cyan] {obs_status}\n"
            "Enter to send, Esc+Enter for newline, Ctrl+C to cancel.\n"
            "Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit.",
            border_style="green",
        )
    )

    session_status = "completed"
    try:
        while True:
            try:
                console.print()
                user_input = await prompt_session.prompt_async(
                    HTML("<ansigreen><b>&gt;</b></ansigreen> ")
                )
                user_input = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                break

            if user_input.startswith("/"):
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
                console.print("\n[yellow]Cancelling...[/yellow]")
                agent.request_cancel()

            # Register signal handler for this turn (Unix only)
            if platform.system() != "Windows":
                loop.add_signal_handler(signal.SIGINT, on_cancel)

            try:
                # Wrap event stream with observability if enabled
                events = agent.run_turn(user_input)
                if observability:
                    events = observability.wrap_turn(events, user_input)

                async for event in events:
                    if event.type == "cancelled":
                        # Check if this was an external kill signal
                        if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                            session_status = "cancelled"
                            if observability:
                                observability.context.metadata["cancel_source"] = "kill_command"
                            console.print("[yellow]Killed by rho-agent kill[/yellow]")
                        else:
                            console.print("[dim]Turn cancelled[/dim]")
                        break
                    handle_event(event)
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
            console.print("\n[dim]Goodbye![/dim]")


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

    def on_cancel():
        console.print("\n[yellow]Cancelling...[/yellow]")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        # Wrap event stream with observability if enabled
        events = agent.run_turn(prompt)
        if observability:
            events = observability.wrap_turn(events, prompt)

        async for event in events:
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if observability:
                        observability.context.metadata["cancel_source"] = "kill_command"
                    console.print("[yellow]Killed by rho-agent kill[/yellow]")
                else:
                    console.print("[dim]Cancelled[/dim]")
                break
            handle_event(event)
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
        console.print(f"[red]Output file already exists: {output_file}[/red]")
        console.print(
            "[dim]Use a different path or delete the existing file first.[/dim]"
        )
        return False

    # Start observability session if enabled
    if observability:
        await observability.start_session()

    collected_text: list[str] = []
    cancelled = False

    loop = asyncio.get_event_loop()

    def on_cancel():
        console.print("\n[yellow]Cancelling...[/yellow]")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        # Wrap event stream with observability if enabled
        events = agent.run_turn(prompt)
        if observability:
            events = observability.wrap_turn(events, prompt)

        async for event in events:
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if observability:
                        observability.context.metadata["cancel_source"] = "kill_command"
                    console.print("[yellow]Killed by rho-agent kill[/yellow]")
                else:
                    console.print("[dim]Cancelled[/dim]")
                cancelled = True
                break
            handle_event(event)
            # Collect text for output file
            if event.type == "text" and event.content:
                collected_text.append(event.content)
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
        console.print(f"\n[green]Output written to: {output_file}[/green]")
        return True
    except Exception as exc:
        console.print(f"\n[red]Failed to write output: {exc}[/red]")
        return False


@app.command()
def main(
    prompt: Annotated[
        Optional[str],
        typer.Argument(help="Single prompt to run (omit for interactive mode)"),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model to use"),
    ] = os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="API base URL for OpenAI-compatible endpoints"),
    ] = os.getenv("OPENAI_BASE_URL"),
    system: Annotated[
        Optional[str],
        typer.Option("--system", "-s", help="Override system prompt entirely"),
    ] = None,
    prompt_file: Annotated[
        Optional[str],
        typer.Option(
            "--prompt", "-p", help="Markdown prompt file with YAML frontmatter"
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
    file_write_mode: Annotated[
        Optional[str],
        typer.Option(
            "--file-write-mode",
            help="Override file write mode: 'off', 'create-only', or 'full'",
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
    """rho-agent: A read-only research assistant."""
    # Set preview lines for tool output display
    global TOOL_PREVIEW_LINES
    TOOL_PREVIEW_LINES = preview_lines

    # Initialize conversation store
    conversation_store = ConversationStore(CONFIG_DIR)

    # Handle --list: show saved conversations and exit
    if list_conversations:
        conversations = conversation_store.list_conversations()
        if not conversations:
            console.print("[dim]No saved conversations found.[/dim]")
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
                f"[cyan]{conv.id}[/cyan]  {time_str}  [dim]{conv.model}[/dim]"
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
                console.print("[red]No saved conversations to resume.[/red]")
                raise typer.Exit(1)
        else:
            conversation_id = resume

        resumed_conversation = conversation_store.load(conversation_id)
        if not resumed_conversation:
            console.print(f"[red]Conversation not found: {conversation_id}[/red]")
            console.print("[dim]Use --list to see saved conversations.[/dim]")
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
            console.print(f"[red]Profile error: {e}[/red]")
            raise typer.Exit(1) from e
    else:
        capability_profile = CapabilityProfile.readonly()

    # Apply command-line overrides to profile
    if shell_mode:
        try:
            capability_profile.shell = ShellMode(shell_mode)
        except ValueError:
            console.print(f"[red]Invalid shell mode: {shell_mode}. Use 'restricted' or 'unrestricted'[/red]")
            raise typer.Exit(1)

    if file_write_mode:
        try:
            capability_profile.file_write = FileWriteMode(file_write_mode)
        except ValueError:
            console.print(f"[red]Invalid file write mode: {file_write_mode}. Use 'off', 'create-only', or 'full'[/red]")
            raise typer.Exit(1)

    # Build system prompt and initial prompt (skip if resuming)
    initial_prompt: str | None = None
    system_prompt: str = ""

    if resumed_conversation:
        # System prompt will be loaded from the conversation
        pass
    elif system:
        # --system overrides everything
        system_prompt = system
    elif prompt_file:
        # --prompt loads markdown file as system prompt
        try:
            loaded_prompt = load_prompt(prompt_file)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        # Collect variables from --vars-file and --var flags
        prompt_vars: dict[str, str] = {}

        if vars_file:
            vars_path = Path(vars_file).expanduser().resolve()
            if not vars_path.exists():
                console.print(f"[red]Vars file not found: {vars_path}[/red]")
                raise typer.Exit(1)
            try:
                with open(vars_path, encoding="utf-8") as f:
                    file_vars = yaml.safe_load(f)
                if isinstance(file_vars, dict):
                    prompt_vars.update({k: str(v) for k, v in file_vars.items()})
            except Exception as exc:
                console.print(f"[red]Failed to load vars file: {exc}[/red]")
                raise typer.Exit(1) from exc

        # --var flags override vars file
        if var:
            try:
                prompt_vars.update(parse_vars(var))
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc

        # Prepare the prompt
        try:
            system_prompt, initial_prompt = prepare_prompt(loaded_prompt, prompt_vars)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    elif DEFAULT_PROMPT_FILE.exists():
        # User's custom default prompt
        try:
            loaded_prompt = load_prompt(DEFAULT_PROMPT_FILE)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        # Provide profile-aware variables (same as builtin)
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
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    else:
        # Built-in default prompt with profile-aware variables
        try:
            loaded_prompt = load_prompt(BUILTIN_PROMPT_FILE)
        except (FileNotFoundError, ValueError) as exc:
            # Fallback to inline prompt if file is missing
            console.print(f"[yellow]Warning: Could not load default prompt: {exc}[/yellow]")
            system_prompt = DEFAULT_SYSTEM_PROMPT.format(
                platform=platform.system(),
                home_dir=str(Path.home()),
                working_dir=resolved_working_dir,
            )
            loaded_prompt = None

        if loaded_prompt:
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
                console.print(f"[red]Prompt error: {exc}[/red]")
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
            if model != os.getenv("OPENAI_MODEL", "gpt-5-nano")
            else resumed_conversation.model
        )
        # Parse the original start time
        session_started = datetime.fromisoformat(resumed_conversation.started)
        console.print(f"[green]Resuming conversation: {conversation_id}[/green]")
    else:
        session = Session(system_prompt=system_prompt)
        effective_model = model

    registry = create_registry(working_dir=resolved_working_dir, profile=capability_profile)
    client = ModelClient(model=effective_model, base_url=base_url)
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
            console.print(f"[yellow]Warning: Failed to initialize observability: {e}[/yellow]")

    # Determine the prompt to run
    # Positional prompt overrides template's initial_prompt
    run_prompt = prompt if prompt else initial_prompt

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
            # Single prompt mode (from --prompt or positional arg)
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
        console.print(f"[red]Dashboard app not found at {dashboard_path}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Starting dashboard on port {port}...[/green]")
    console.print(f"[dim]Database: {resolved_db}[/dim]")
    console.print(f"[dim]Open http://localhost:{port} in your browser[/dim]")

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
        console.print("\n[dim]Dashboard stopped[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Dashboard failed to start: {e}[/red]")
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
            console.print(f"[green]Removed {len(cleaned)} stale entries[/green]")
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
            f"  [cyan]{short_id}[/cyan]  [green]running[/green]  "
            f"[dim]{info.model:<14}[/dim]  {duration:>6}  {preview}"
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
                console.print(f"[yellow]Cancelled: {sid[:8]}[/yellow]")
            console.print(f"[green]Sent cancel signal to {len(cancelled)} agents[/green]")
        else:
            console.print("[dim]No running agents to kill[/dim]")
        return

    if not prefix:
        console.print("[red]Provide a session ID prefix, or use --all[/red]")
        raise typer.Exit(1)

    cancelled = sm.cancel_by_prefix(prefix)
    if cancelled:
        for sid in cancelled:
            console.print(f"[yellow]Cancelled: {sid[:8]}[/yellow]")
    else:
        console.print(f"[red]No running agents matching prefix '{prefix}'[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
