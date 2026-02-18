"""Main CLI command: rho-agent entry point."""

import asyncio
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
import yaml
from dotenv import load_dotenv

load_dotenv()

from ..capabilities import CapabilityProfile, ShellMode
from ..capabilities.factory import load_profile
from ..core.conversations import ConversationStore
from ..core.session import Session
from ..runtime import ObservabilityInitializationError, RuntimeOptions, create_runtime
from ..signals import AgentInfo, SignalManager
from ..ui.theme import THEME
from .errors import (
    InvalidModeError,
    InvalidProfileError,
    MissingApiKeyError,
    PromptLoadError,
)
from .events import ApprovalHandler
from .formatting import (
    _format_observability_init_error,
    _is_interactive_terminal,
    _markup,
)
from .interactive import run_interactive
from .single import run_single, run_single_with_output
from .state import (
    BUILTIN_PROMPT_FILE,
    CONFIG_DIR,
    DEFAULT_PROMPT_FILE,
    app,
    console,
    settings,
)


@app.command()
def main(
    prompt_arg: Annotated[
        str | None,
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
        str | None,
        typer.Option("--base-url", help="API base URL for OpenAI-compatible endpoints"),
    ] = os.getenv("OPENAI_BASE_URL"),
    reasoning_effort: Annotated[
        str | None,
        typer.Option("--reasoning-effort", help="Reasoning effort: low, medium, high"),
    ] = os.getenv("RHO_AGENT_REASONING_EFFORT"),
    system_prompt_file: Annotated[
        str | None,
        typer.Option(
            "--system-prompt",
            "-s",
            help="Markdown system prompt file with YAML frontmatter",
        ),
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            "-p",
            help="High-level prompt text for one-shot mode (will use default system prompt unless specified)",
        ),
    ] = None,
    var: Annotated[
        list[str] | None,
        typer.Option("--var", help="Prompt variable (key=value, repeatable)"),
    ] = None,
    vars_file: Annotated[
        str | None,
        typer.Option("--vars-file", help="YAML file with prompt variables"),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write final response to file"),
    ] = None,
    working_dir: Annotated[
        str | None,
        typer.Option("--working-dir", "-w", help="Working directory for shell commands"),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", "-y", help="Auto-approve all tool calls"),
    ] = False,
    resume: Annotated[
        str | None,
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
        str | None,
        typer.Option(
            "--profile",
            help="Capability profile: 'readonly' (default), 'developer', 'eval', or path to YAML",
        ),
    ] = os.getenv("RHO_AGENT_PROFILE"),
    shell_mode: Annotated[
        str | None,
        typer.Option(
            "--shell-mode",
            help="Override shell mode: 'restricted' or 'unrestricted'",
        ),
    ] = None,
    team_id: Annotated[
        str | None,
        typer.Option(
            "--team-id",
            help="Team ID for observability (enables telemetry)",
        ),
    ] = os.getenv("RHO_AGENT_TEAM_ID"),
    project_id: Annotated[
        str | None,
        typer.Option(
            "--project-id",
            help="Project ID for observability (enables telemetry)",
        ),
    ] = os.getenv("RHO_AGENT_PROJECT_ID"),
    observability_config: Annotated[
        str | None,
        typer.Option(
            "--observability-config",
            help="Path to observability config file",
        ),
    ] = os.getenv("RHO_AGENT_OBSERVABILITY_CONFIG"),
) -> None:
    """rho-agent: An agent harness and CLI with readonly and developer modes."""
    from ..prompts import load_prompt, parse_vars, prepare_prompt

    # Set preview lines for tool output display
    settings.tool_preview_lines = preview_lines

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
