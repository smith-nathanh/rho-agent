"""CLI entry point for ro-agent."""

import asyncio
import os
import platform
import re
import signal
from datetime import datetime
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
from .templates import load_template, prepare_template
from .templates.renderer import parse_vars
from .tools.handlers import (
    FindFilesHandler,
    ListDirHandler,
    OracleHandler,
    ReadExcelHandler,
    ReadFileHandler,
    SearchHandler,
    ShellHandler,
    SqliteHandler,
    VerticaHandler,
    WriteOutputHandler,
)
from .tools.registry import ToolRegistry

# Config directory for ro-agent data
CONFIG_DIR = Path.home() / ".config" / "ro-agent"
HISTORY_FILE = CONFIG_DIR / "history"
PROMPT_CONFIG_FILE = CONFIG_DIR / "prompts.yaml"

# Tool output preview lines (0 to disable)
TOOL_PREVIEW_LINES = int(os.getenv("RO_AGENT_PREVIEW_LINES", "6"))

# Rich console for all output
console = Console()

# Typer app
app = typer.Typer(
    name="ro-agent",
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
COMMANDS = ["/approve", "/compact", "/help", "/clear", "/prompt", "exit", "quit"]

# Pattern to detect path-like strings in text
PATH_PATTERN = re.compile(
    r"(~/?|\.{1,2}/|/)?([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-.]*|~[a-zA-Z0-9_\-./]*)$"
)


def load_prompt_config(path: Path) -> dict[str, Any] | None:
    """Load prompt config YAML from disk."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to read prompt config: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Prompt config must be a YAML mapping at the top level")
    return data


def build_repo_context(profile: dict[str, Any], working_dir: str) -> str:
    """Build repo context text from inline content and files."""
    parts: list[str] = []
    inline = profile.get("repo_context")
    if isinstance(inline, str) and inline.strip():
        parts.append(inline.strip())

    files = profile.get("repo_context_files", [])
    if files:
        if not isinstance(files, list):
            raise ValueError("repo_context_files must be a list of file paths")
        for entry in files:
            if not isinstance(entry, str):
                continue
            path = Path(entry)
            if not path.is_absolute():
                path = Path(working_dir) / path
            if not path.exists():
                console.print(f"[yellow]Repo context file not found: {path}[/yellow]")
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception as exc:
                console.print(
                    f"[yellow]Failed to read repo context file {path}: {exc}[/yellow]"
                )
                continue
            if content:
                parts.append(f"### {path}\n{content}")
    return "\n\n".join(parts).strip()


def build_system_prompt_from_profile(
    profile_name: str,
    config: dict[str, Any],
    working_dir: str,
) -> str:
    """Build system prompt from a named profile."""
    profiles = config.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be a mapping of profile names to configs")
    if profile_name not in profiles:
        raise ValueError(f"Profile '{profile_name}' not found in prompt config")
    profile = profiles[profile_name]
    if not isinstance(profile, dict):
        raise ValueError(f"Profile '{profile_name}' must be a mapping")
    template = profile.get("system_prompt")
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f"Profile '{profile_name}' is missing system_prompt text")

    repo_context = build_repo_context(profile, working_dir)

    # Reserved keys that have special meaning
    reserved_keys = {"system_prompt", "repo_context", "repo_context_files"}

    # Start with built-in variables
    format_vars: dict[str, Any] = {
        "platform": platform.system(),
        "home_dir": str(Path.home()),
        "working_dir": working_dir,
        "repo_context": repo_context,
    }

    # Add all custom keys from the profile as additional variables
    for key, value in profile.items():
        if key not in reserved_keys and isinstance(value, str):
            format_vars[key] = value

    try:
        prompt = template.format(**format_vars)
    except KeyError as exc:
        raise ValueError(f"Unknown placeholder in system_prompt: {exc}") from exc

    if repo_context and "{repo_context}" not in template:
        prompt = f"{prompt}\n\n## Repo Context\n{repo_context}"
    return prompt


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


def create_registry(working_dir: str | None = None) -> ToolRegistry:
    """Create and configure the tool registry."""
    registry = ToolRegistry()
    # Dedicated read-only tools (preferred for inspection)
    registry.register(ReadFileHandler())
    registry.register(ReadExcelHandler())
    registry.register(ListDirHandler())
    registry.register(FindFilesHandler())
    registry.register(SearchHandler())
    # Shell for commands that need it (jq, custom tools, etc.)
    registry.register(ShellHandler(working_dir=working_dir))
    # Output tool for exporting findings
    registry.register(WriteOutputHandler())

    # Database handlers - register if configured via env vars
    if os.environ.get("ORACLE_DSN"):
        registry.register(OracleHandler())
    if os.environ.get("SQLITE_DB"):
        registry.register(SqliteHandler())
    if os.environ.get("VERTICA_HOST"):
        registry.register(VerticaHandler())

    return registry


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
    """Format tool call as a signature like: read_file(path='/foo/bar.py')"""
    if not tool_args:
        return f"{tool_name}()"

    # For shell commands, show just the command
    if tool_name == "shell" and "command" in tool_args:
        return f"shell({tool_args['command']})"

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
        if tool_name == "search":
            matches = metadata.get("matches", 0)
            truncated = metadata.get("truncated", False)
            if matches:
                suffix = "+" if truncated else ""
                return f"{matches}{suffix} matches"
            return "No matches"

        if tool_name == "read_file":
            total = metadata.get("total_lines", 0)
            start = metadata.get("start_line", 1)
            end = metadata.get("end_line", total)
            if total:
                return f"Read lines {start}-{end} of {total}"

        if tool_name == "list_dir":
            count = metadata.get("item_count", 0)
            if count:
                return f"{count} items"

        if tool_name == "write_output":
            size = metadata.get("size_bytes", 0)
            lines = metadata.get("lines", 0)
            path = metadata.get("path", "")
            if size:
                return f"Wrote {size} bytes ({lines} lines)"

        if tool_name in ("oracle", "sqlite", "vertica"):
            rows = metadata.get("row_count", metadata.get("table_count", 0))
            if rows:
                return f"{rows} rows"

    # Fallback: count lines in result
    if result:
        lines = result.count("\n") + 1
        if lines > 1:
            return f"{lines} lines"

    return None


def _format_tool_preview(result: str | None, max_lines: int | None = None) -> str | None:
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
        summary = _format_tool_summary(event.tool_name, event.tool_metadata, event.tool_result)
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
    session: Session,
    prompt_config_path: Path,
    working_dir: str,
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

    if cmd.startswith("/prompt"):
        parts = cmd.split(maxsplit=1)
        try:
            config = load_prompt_config(prompt_config_path)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
        if not config:
            console.print(
                f"[yellow]Prompt config not found: {prompt_config_path}[/yellow]"
            )
            return None
        profiles = config.get("profiles", {})
        if len(parts) == 1:
            if not isinstance(profiles, dict) or not profiles:
                console.print("[yellow]No prompt profiles found.[/yellow]")
                return None
            default_name = config.get("default")
            lines = ["[bold]Prompt profiles:[/bold]"]
            for name in sorted(profiles.keys()):
                suffix = " (default)" if name == default_name else ""
                lines.append(f"  {name}{suffix}")
            console.print(Panel("\n".join(lines), title="Prompts", border_style="blue"))
            return None

        profile_name = parts[1].strip()
        if not profile_name:
            console.print("[yellow]Usage: /prompt <profile>[/yellow]")
            return None
        try:
            session.system_prompt = build_system_prompt_from_profile(
                profile_name, config, working_dir
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return None
        console.print(f"[green]System prompt set to profile '{profile_name}'.[/green]")
        console.print(
            "[dim]Tip: use /compact or start a new session to align history.[/dim]"
        )
        return None

    if cmd == "/help":
        console.print(
            Panel(
                "[bold]Commands:[/bold]\n"
                "  /approve             - Enable auto-approve for all tool calls\n"
                "  /compact [guidance]  - Compact conversation history\n"
                "  /prompt [name]       - List or switch prompt profile\n"
                "  /help                - Show this help\n"
                "  /clear               - Clear the screen\n"
                "  exit                 - Quit the session\n"
                "\n[bold]Input:[/bold]\n"
                "  Enter               - Send message\n"
                "  Esc+Enter           - New line\n"
                "\n[bold]Conversations:[/bold]\n"
                "  ro-agent --list     - List saved conversations\n"
                "  ro-agent -r latest  - Resume most recent conversation\n"
                "  ro-agent -r <id>    - Resume specific conversation",
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
    prompt_config_path: Path,
    conversation_store: ConversationStore,
    session_started: datetime,
    conversation_id: str | None = None,
) -> None:
    """Run an interactive REPL session."""
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

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
    console.print(
        Panel(
            "[bold]ro-agent[/bold] - Read-only research assistant\n"
            f"Model: [cyan]{model}[/cyan]\n"
            "Enter to send, Esc+Enter for newline, Ctrl+C to cancel.\n"
            "Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit.",
            border_style="green",
        )
    )

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
            action = handle_command(
                user_input, approval_handler, session, prompt_config_path, working_dir
            )
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
            async for event in agent.run_turn(user_input):
                if event.type == "cancelled":
                    console.print("[dim]Turn cancelled[/dim]")
                    break
                handle_event(event)
        finally:
            # Remove signal handler after turn
            if platform.system() != "Windows":
                loop.remove_signal_handler(signal.SIGINT)

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


async def run_single(agent: Agent, prompt: str) -> None:
    """Run a single prompt and exit."""
    loop = asyncio.get_event_loop()

    def on_cancel():
        console.print("\n[yellow]Cancelling...[/yellow]")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    try:
        async for event in agent.run_turn(prompt):
            if event.type == "cancelled":
                console.print("[dim]Cancelled[/dim]")
                break
            handle_event(event)
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)


async def run_single_with_output(agent: Agent, prompt: str, output_path: str) -> bool:
    """Run a single prompt and write final response to file.

    Returns True if successful, False if output file already exists.
    """
    output_file = Path(output_path).expanduser().resolve()

    # Check if output file already exists before running
    if output_file.exists():
        console.print(f"[red]Output file already exists: {output_file}[/red]")
        console.print("[dim]Use a different path or delete the existing file first.[/dim]")
        return False

    collected_text: list[str] = []
    cancelled = False

    loop = asyncio.get_event_loop()

    def on_cancel():
        console.print("\n[yellow]Cancelling...[/yellow]")
        agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    try:
        async for event in agent.run_turn(prompt):
            if event.type == "cancelled":
                console.print("[dim]Cancelled[/dim]")
                cancelled = True
                break
            handle_event(event)
            # Collect text for output file
            if event.type == "text" and event.content:
                collected_text.append(event.content)
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)

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
    ] = os.getenv("OPENAI_MODEL", "gpt-5.1"),
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="API base URL for OpenAI-compatible endpoints"),
    ] = os.getenv("OPENAI_BASE_URL"),
    system: Annotated[
        Optional[str],
        typer.Option("--system", "-s", help="System prompt"),
    ] = None,
    prompt_config: Annotated[
        Optional[str],
        typer.Option("--prompt-config", help="Path to prompt config YAML"),
    ] = None,
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", help="Prompt profile name from config"),
    ] = None,
    template: Annotated[
        Optional[str],
        typer.Option("--template", "-t", help="Template name for dispatch mode"),
    ] = None,
    var: Annotated[
        Optional[list[str]],
        typer.Option("--var", help="Template variable (key=value, repeatable)"),
    ] = None,
    vars_file: Annotated[
        Optional[str],
        typer.Option("--vars-file", help="YAML file with template variables"),
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
        typer.Option("--preview-lines", help="Lines of tool output to show (0 to disable)"),
    ] = int(os.getenv("RO_AGENT_PREVIEW_LINES", "6")),
) -> None:
    """ro-agent: A read-only research assistant."""
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
            console.print(f"[cyan]{conv.id}[/cyan]  {time_str}  [dim]{conv.model}[/dim]")
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

    prompt_config_path = (
        Path(prompt_config).expanduser().resolve()
        if prompt_config
        else PROMPT_CONFIG_FILE
    )

    # Track when session started
    session_started = datetime.now()

    # Build system prompt and initial prompt (skip if resuming)
    initial_prompt: str | None = None
    system_prompt: str = ""

    if resumed_conversation:
        # System prompt will be loaded from the conversation
        pass
    elif template:
        try:
            tmpl = load_template(template)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        # Collect variables from --vars-file and --var flags
        template_vars: dict[str, str] = {}

        if vars_file:
            vars_path = Path(vars_file).expanduser().resolve()
            if not vars_path.exists():
                console.print(f"[red]Vars file not found: {vars_path}[/red]")
                raise typer.Exit(1)
            try:
                with open(vars_path, encoding="utf-8") as f:
                    file_vars = yaml.safe_load(f)
                if isinstance(file_vars, dict):
                    template_vars.update({k: str(v) for k, v in file_vars.items()})
            except Exception as exc:
                console.print(f"[red]Failed to load vars file: {exc}[/red]")
                raise typer.Exit(1) from exc

        # --var flags override vars file
        if var:
            try:
                template_vars.update(parse_vars(var))
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc

        # Prepare the template
        try:
            system_prompt, initial_prompt = prepare_template(tmpl, template_vars)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    elif system:
        system_prompt = system
    else:
        config = None
        try:
            config = load_prompt_config(prompt_config_path)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        profile_name = profile
        if not profile_name and config and config.get("default"):
            profile_name = str(config.get("default"))

        if profile_name:
            if not config:
                console.print(
                    f"[red]Prompt profile requested but config not found: {prompt_config_path}[/red]"
                )
                raise typer.Exit(1)
            try:
                system_prompt = build_system_prompt_from_profile(
                    profile_name, config, resolved_working_dir
                )
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
        else:
            system_prompt = DEFAULT_SYSTEM_PROMPT.format(
                platform=platform.system(),
                home_dir=str(Path.home()),
                working_dir=resolved_working_dir,
            )

    # Set up components - use resumed conversation if available
    if resumed_conversation:
        session = Session(system_prompt=resumed_conversation.system_prompt)
        session.history = resumed_conversation.history.copy()
        session.total_input_tokens = resumed_conversation.input_tokens
        session.total_output_tokens = resumed_conversation.output_tokens
        # Use the model from resumed conversation unless explicitly overridden
        effective_model = model if model != os.getenv("OPENAI_MODEL", "gpt-5-nano") else resumed_conversation.model
        # Parse the original start time
        session_started = datetime.fromisoformat(resumed_conversation.started)
        console.print(f"[green]Resuming conversation: {conversation_id}[/green]")
    else:
        session = Session(system_prompt=system_prompt)
        effective_model = model

    registry = create_registry(working_dir=resolved_working_dir)
    client = ModelClient(model=effective_model, base_url=base_url)
    approval_handler = ApprovalHandler(auto_approve=auto_approve)

    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=approval_handler.check_approval,
    )

    # Determine the prompt to run
    # Positional prompt overrides template's initial_prompt
    run_prompt = prompt if prompt else initial_prompt

    if run_prompt:
        # Single prompt mode (from --template or positional arg)
        if output:
            # Capture output to file
            asyncio.run(run_single_with_output(agent, run_prompt, output))
        else:
            asyncio.run(run_single(agent, run_prompt))
    else:
        asyncio.run(
            run_interactive(
                agent,
                approval_handler,
                session,
                effective_model,
                resolved_working_dir,
                prompt_config_path,
                conversation_store,
                session_started,
                conversation_id,
            )
        )


if __name__ == "__main__":
    app()
