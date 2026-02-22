"""Main CLI command: rho-agent entry point."""

from __future__ import annotations

import asyncio
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from dotenv import load_dotenv

load_dotenv()

from ..permissions import PermissionProfile, ShellMode
from ..permissions.factory import load_profile
from ..core import Agent, AgentConfig, Session, SessionStore
from .theme import THEME
from .errors import (
    InvalidModeError,
    InvalidProfileError,
    MissingApiKeyError,
    PromptLoadError,
)
from .events import ApprovalHandler
from .formatting import (
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


SESSIONS_DIR = CONFIG_DIR / "sessions"


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
    config_file: Annotated[
        str | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to agent config YAML file",
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
            help="Resume a session (use 'latest' or a session ID/path)",
        ),
    ] = None,
    list_sessions: Annotated[
        bool,
        typer.Option("--list", "-l", help="List saved sessions and exit"),
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
    backend: Annotated[
        str,
        typer.Option("--backend", help="Execution backend: 'local' or 'daytona'"),
    ] = os.getenv("RHO_AGENT_BACKEND", "local"),
    upload: Annotated[
        list[str] | None,
        typer.Option(
            "--upload", help="Upload files to sandbox (repeatable, format: ./local:/remote)"
        ),
    ] = None,
    shell_mode: Annotated[
        str | None,
        typer.Option(
            "--shell-mode",
            help="Override shell mode: 'restricted' or 'unrestricted'",
        ),
    ] = None,
) -> None:
    """rho-agent: An agent harness and CLI with readonly and developer modes."""
    from ..prompts import load_prompt, parse_vars, prepare_prompt

    # Parse upload mappings (format: ./local:/remote)
    upload_mappings: list[tuple[str, str]] = []
    if upload:
        for mapping in upload:
            if ":" not in mapping:
                console.print(
                    _markup(
                        f"Invalid --upload format (expected ./local:/remote): {mapping}",
                        THEME.error,
                    )
                )
                raise typer.Exit(1)
            src, dest = mapping.rsplit(":", 1)
            upload_mappings.append((src, dest))

    # Set preview lines for tool output display
    settings.tool_preview_lines = preview_lines

    # Initialize session store
    session_store = SessionStore(SESSIONS_DIR)

    # Handle --list: show saved sessions and exit
    if list_sessions:
        sessions = session_store.list()
        if not sessions:
            console.print(_markup("No saved sessions found.", THEME.muted))
            raise typer.Exit(0)

        console.print("[bold]Saved sessions:[/bold]\n")
        for info in sessions:
            try:
                started_dt = datetime.fromisoformat(info.created_at)
                time_str = started_dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                time_str = info.id
            console.print(
                f"{_markup(info.id, THEME.accent)}  {time_str}  {_markup(info.model, THEME.muted)}"
            )
            console.print(f"  {info.display_preview}")
            console.print()
        raise typer.Exit(0)

    # Handle --resume: load a previous session
    resumed_session: Session | None = None
    if resume:
        if resume.lower() == "latest":
            latest_id = session_store.get_latest_id()
            if not latest_id:
                console.print(_markup("No saved sessions to resume.", THEME.error))
                raise typer.Exit(1)
            resume = latest_id

        try:
            # Build override config if CLI flags differ from defaults
            override_config = None
            if model != os.getenv("OPENAI_MODEL", "gpt-5-mini"):
                # User explicitly set --model, use it as override
                override_config = AgentConfig(model=model)
            resumed_session = session_store.resume(resume, agent_config=override_config)
        except FileNotFoundError:
            console.print(_markup(f"Session not found: {resume}", THEME.error))
            console.print(_markup("Use --list to see saved sessions.", THEME.muted))
            raise typer.Exit(1)

    # Resolve working directory
    resolved_working_dir = (
        str(Path(working_dir).expanduser().resolve()) if working_dir else os.getcwd()
    )

    # Build AgentConfig from flags or --config file
    if resumed_session:
        agent = resumed_session.agent
        session = resumed_session
        capability_profile = load_profile(agent.config.profile)
        mode_name = capability_profile.name
        console.print(_markup(f"Resuming session: {session.id}", THEME.success))
    elif config_file:
        # Load from config file with CLI overrides
        agent_config = AgentConfig.from_file(config_file)
        if model != os.getenv("OPENAI_MODEL", "gpt-5-mini"):
            agent_config.model = model
        if profile:
            agent_config.profile = profile
        if backend != "local":
            agent_config.backend = backend
        if base_url:
            agent_config.base_url = base_url
        if reasoning_effort:
            agent_config.reasoning_effort = reasoning_effort
        if working_dir:
            agent_config.working_dir = resolved_working_dir
        agent_config.auto_approve = auto_approve

        try:
            agent = Agent(agent_config)
        except (ValueError, FileNotFoundError) as e:
            console.print(_markup(str(InvalidProfileError(str(e))), THEME.error))
            raise typer.Exit(1) from e

        capability_profile = load_profile(agent_config.profile)
        mode_name = capability_profile.name
        session = session_store.create_session(agent)
    else:
        # Build config from individual CLI flags
        effective_profile = profile or "readonly"

        # Load capability profile (for validation and prompt rendering)
        try:
            capability_profile = load_profile(effective_profile)
        except (ValueError, FileNotFoundError) as e:
            console.print(_markup(str(InvalidProfileError(str(e))), THEME.error))
            raise typer.Exit(1) from e
        mode_name = capability_profile.name

        # Apply --shell-mode override
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

        # Resolve system prompt
        system_prompt_text: str = ""
        initial_prompt: str | None = None

        if system_prompt_file:
            try:
                loaded_prompt = load_prompt(system_prompt_file)
            except (FileNotFoundError, ValueError) as exc:
                console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
                raise typer.Exit(1) from exc

            prompt_vars: dict[str, str] = {}
            if vars_file:
                vars_path = Path(vars_file).expanduser().resolve()
                if not vars_path.exists():
                    console.print(
                        _markup(
                            str(PromptLoadError(f"Vars file not found: {vars_path}")), THEME.error
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
                        _markup(
                            str(PromptLoadError(f"Failed to load vars file: {exc}")), THEME.error
                        )
                    )
                    raise typer.Exit(1) from exc

            if var:
                try:
                    prompt_vars.update(parse_vars(var))
                except ValueError as exc:
                    console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
                    raise typer.Exit(1) from exc

            try:
                system_prompt_text, initial_prompt = prepare_prompt(loaded_prompt, prompt_vars)
            except ValueError as exc:
                console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
                raise typer.Exit(1) from exc
        else:
            # Use default prompt
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
                system_prompt_text, initial_prompt = prepare_prompt(loaded_prompt, prompt_vars)
            except ValueError as exc:
                console.print(_markup(f"Prompt error: {PromptLoadError(str(exc))}", THEME.error))
                raise typer.Exit(1) from exc

        agent_config = AgentConfig(
            system_prompt=system_prompt_text,
            model=model,
            profile=effective_profile,
            backend=backend,
            base_url=base_url,
            reasoning_effort=reasoning_effort,
            working_dir=resolved_working_dir,
            auto_approve=auto_approve,
        )
        agent = Agent(agent_config)

        # Apply shell_mode override to the profile used by the registry
        if shell_mode:
            # Re-build registry with overridden profile
            from ..permissions.factory import ToolFactory

            registry = ToolFactory(capability_profile).create_registry(
                working_dir=resolved_working_dir
            )
            agent._registry = registry

        session = session_store.create_session(agent)

    # Register delegate handler if profile supports it
    if capability_profile.name not in ("birdbench",):
        _register_delegate(agent, session)

    if not agent.config.base_url and not os.getenv("OPENAI_API_KEY"):
        console.print(_markup(str(MissingApiKeyError()), THEME.error))
        raise typer.Exit(1)

    # Determine the prompt to run
    run_prompt_text = (
        prompt
        if prompt
        else (prompt_arg if prompt_arg else (initial_prompt if not resumed_session else None))
    )

    if not run_prompt_text and not _is_interactive_terminal():
        console.print(
            _markup(
                "Non-interactive terminal detected. Provide a prompt argument, or run in a TTY for interactive mode.",
                THEME.warning,
            )
        )
        raise typer.Exit(1)

    # Set up approval handler
    approval_handler = ApprovalHandler(auto_approve=auto_approve)
    session.approval_callback = approval_handler.check_approval

    if run_prompt_text:
        console.print(_markup(f"Mode: {mode_name}", THEME.accent))
        if output:
            asyncio.run(
                run_single_with_output(
                    session,
                    run_prompt_text,
                    output,
                    upload_mappings=upload_mappings,
                )
            )
        else:
            asyncio.run(
                run_single(
                    session,
                    run_prompt_text,
                    upload_mappings=upload_mappings,
                )
            )
    else:
        asyncio.run(
            run_interactive(
                session,
                approval_handler,
                mode_name,
                resolved_working_dir,
                session_store,
                upload_mappings=upload_mappings,
            )
        )


def _register_delegate(agent: Agent, session: Session) -> None:
    """Register the delegate handler if the profile supports it."""
    from ..permissions.factory import load_profile
    from ..tools.handlers.delegate import DelegateHandler

    try:
        profile = load_profile(agent.config.profile)
    except (ValueError, FileNotFoundError):
        return

    if not hasattr(profile, "requires_tool_approval"):
        return

    agent.registry.register(
        DelegateHandler(
            parent_config=agent.config,
            parent_system_prompt=agent.system_prompt,
            parent_state=session.state,
            parent_approval_callback=session.approval_callback,
            parent_cancel_check=session.cancel_check,
            requires_approval=profile.requires_tool_approval("delegate"),
        )
    )
