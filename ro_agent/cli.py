"""CLI entry point for ro-agent."""

import asyncio
import os
import platform
import re
from pathlib import Path
from typing import Annotated, Any, Iterable, Optional

import typer
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
from rich.console import Console
from rich.panel import Panel

from .client.model import ModelClient
from .core.agent import Agent, AgentEvent
from .core.session import Session
from .tools.handlers import GrepFilesHandler, ListDirHandler, ReadFileHandler, ShellHandler
from .tools.registry import ToolRegistry

# Config directory for ro-agent data
CONFIG_DIR = Path.home() / ".config" / "ro-agent"
HISTORY_FILE = CONFIG_DIR / "history"

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
You have access to tools for investigating issues.
You are read-only - you cannot modify files or execute destructive commands.
Be thorough in your investigation and provide clear summaries of what you find.

## Environment
- Platform: {platform}
- Home directory: {home_dir}
- Working directory: {working_dir}

When users reference paths with ~, expand them to {home_dir}.
Always use absolute paths in tool calls.
"""

# Commands the user can type during the session
COMMANDS = ["/approve", "/help", "/clear", "exit", "quit"]

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


def create_registry(working_dir: str | None = None) -> ToolRegistry:
    """Create and configure the tool registry."""
    registry = ToolRegistry()
    # Dedicated read-only tools (preferred for inspection)
    registry.register(ReadFileHandler())
    registry.register(ListDirHandler())
    registry.register(GrepFilesHandler())
    # Shell for commands that need it (jq, custom tools, etc.)
    registry.register(ShellHandler(working_dir=working_dir))
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


def handle_event(event: AgentEvent) -> None:
    """Handle an agent event by printing to console."""
    if event.type == "text":
        # Stream text immediately as it arrives
        print(event.content or "", end="", flush=True)

    elif event.type == "tool_start":
        # Ensure we're on a new line before showing tool
        print()
        cmd = event.tool_args.get("command", "") if event.tool_args else ""
        console.print(
            Panel(
                cmd or str(event.tool_args),
                title=f"[cyan]{event.tool_name}[/cyan]",
                border_style="cyan",
                expand=False,
            )
        )

    elif event.type == "tool_end":
        result = event.tool_result or ""
        if len(result) > 2000:
            result = result[:2000] + "\n... (truncated)"
        console.print(
            Panel(
                result,
                border_style="dim",
                expand=False,
            )
        )

    elif event.type == "tool_blocked":
        console.print("[red]Command rejected[/red]")

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


def handle_command(cmd: str, approval_handler: ApprovalHandler) -> bool:
    """Handle slash commands. Returns True if should continue loop."""
    if cmd == "/approve":
        approval_handler.enable_auto_approve()
        return True

    if cmd == "/help":
        console.print(
            Panel(
                "[bold]Commands:[/bold]\n"
                "  /approve  - Enable auto-approve for all tool calls\n"
                "  /help     - Show this help\n"
                "  /clear    - Clear the screen\n"
                "  exit      - Quit the session",
                title="Help",
                border_style="blue",
            )
        )
        return True

    if cmd == "/clear":
        console.clear()
        return True

    return True


async def run_interactive(
    agent: Agent,
    approval_handler: ApprovalHandler,
    model: str,
    working_dir: str | None,
) -> None:
    """Run an interactive REPL session."""
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Prompt toolkit session with history and completion
    prompt_session: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=create_completer(working_dir=working_dir),
        complete_while_typing=False,
        complete_in_thread=True,
    )

    # Welcome message
    console.print(
        Panel(
            "[bold]ro-agent[/bold] - Read-only research assistant\n"
            f"Model: [cyan]{model}[/cyan]\n"
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
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            console.print("[dim]Goodbye![/dim]")
            break

        if user_input.startswith("/"):
            handle_command(user_input.lower(), approval_handler)
            continue

        # Run the turn and handle events
        async for event in agent.run_turn(user_input):
            handle_event(event)


async def run_single(agent: Agent, prompt: str) -> None:
    """Run a single prompt and exit."""
    async for event in agent.run_turn(prompt):
        handle_event(event)


@app.command()
def main(
    prompt: Annotated[
        Optional[str],
        typer.Argument(help="Single prompt to run (omit for interactive mode)"),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model to use"),
    ] = os.getenv("OPENAI_MODEL", "gpt-5-nano"),
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="API base URL for OpenAI-compatible endpoints"),
    ] = os.getenv("OPENAI_BASE_URL"),
    system: Annotated[
        Optional[str],
        typer.Option("--system", "-s", help="System prompt"),
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
) -> None:
    """ro-agent: A read-only research assistant."""
    # Resolve working directory
    resolved_working_dir = (
        str(Path(working_dir).expanduser().resolve()) if working_dir else os.getcwd()
    )

    # Build system prompt with environment context
    if system:
        system_prompt = system
    else:
        system_prompt = DEFAULT_SYSTEM_PROMPT.format(
            platform=platform.system(),
            home_dir=str(Path.home()),
            working_dir=resolved_working_dir,
        )

    # Set up components
    session = Session(system_prompt=system_prompt)
    registry = create_registry(working_dir=resolved_working_dir)
    client = ModelClient(model=model, base_url=base_url)
    approval_handler = ApprovalHandler(auto_approve=auto_approve)

    agent = Agent(
        session=session,
        registry=registry,
        client=client,
        approval_callback=approval_handler.check_approval,
    )

    if prompt:
        asyncio.run(run_single(agent, prompt))
    else:
        asyncio.run(run_interactive(agent, approval_handler, model, working_dir))


if __name__ == "__main__":
    app()
