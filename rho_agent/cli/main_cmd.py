"""Main CLI command: rho-agent entry point."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml

if TYPE_CHECKING:
    from ..tools.handlers.daytona.backend import DaytonaBackend

from dotenv import load_dotenv

load_dotenv()

from ..core import Agent, AgentConfig, Session, SessionStore
from ..core.approvals import ApprovalQueue
from ..permissions import ShellMode
from ..permissions.factory import load_profile
from .errors import (
    InvalidModeError,
    InvalidProfileError,
    MissingApiKeyError,
    PromptLoadError,
)
from .events import ApprovalHandler, announce_queued_approval
from .formatting import (
    _is_interactive_terminal,
    _markup,
)
from .interactive import run_interactive
from .single import run_single, run_single_with_output
from .state import (
    BUILTIN_PROMPT_FILE,
    DEFAULT_PROMPT_FILE,
    SESSIONS_DIR,
    app,
    console,
    settings,
)
from .theme import THEME

SANDBOX_CONFIG_FILE = ".rho-agent.toml"

# Config fields the default system prompt describes.
_PROMPT_FIELDS = ("profile", "working_dir", "backend")


def _resolve_daytona_config(
    *,
    dockerfile: str | None,
    snapshot: str | None,
    upload: list[str] | None,
    working_dir: str | None,
) -> tuple[DaytonaBackend, list[tuple[str, str]], str, list[str]]:
    """Resolve Daytona sandbox configuration from .rho-agent.toml + CLI overrides.

    Returns (DaytonaBackend, upload_mappings, working_dir, setup_commands).
    """
    import tomllib

    from ..tools.handlers.daytona.backend import DaytonaBackend

    # Walk up from CWD to find .rho-agent.toml (like .gitignore discovery)
    cwd = Path.cwd()
    project_root = cwd
    config_path = None
    for parent in [cwd, *cwd.parents]:
        candidate = parent / SANDBOX_CONFIG_FILE
        if candidate.exists():
            config_path = candidate
            project_root = parent
            break

    project_name = project_root.name
    remote_project_dir = f"/home/daytona/{project_name}"

    # Load .rho-agent.toml if found
    toml_config: dict = {}
    if config_path is not None:
        with open(config_path, "rb") as f:
            toml_config = tomllib.load(f)
        console.print(_markup(f"Loaded {config_path.relative_to(project_root)}", THEME.muted))

    sandbox_cfg = toml_config.get("sandbox", {})

    # Resolve image/snapshot — CLI flags override toml
    image: str | object = "daytonaio/sandbox:latest"
    resolved_snapshot: str | None = snapshot or sandbox_cfg.get("snapshot")

    if not resolved_snapshot:
        dockerfile_path_str = dockerfile or sandbox_cfg.get("dockerfile")
        if dockerfile_path_str:
            df_path = Path(dockerfile_path_str)
            if not df_path.is_absolute():
                df_path = project_root / df_path
            df_path = df_path.resolve()
            if not df_path.exists():
                console.print(
                    _markup(f"Dockerfile not found: {df_path}", THEME.error)
                )
                raise typer.Exit(1)
            from daytona import Image

            # The build context is the Dockerfile's directory (the SDK's
            # `context_dir` argument no longer exists).
            image = Image.from_dockerfile(str(df_path))
        else:
            toml_image = sandbox_cfg.get("image")
            if toml_image:
                image = toml_image

    # Environment variables from toml
    env_vars = {}
    if "env" in sandbox_cfg:
        env_vars = {k: str(v) for k, v in sandbox_cfg["env"].items()}

    backend = DaytonaBackend(
        image=image,
        snapshot=resolved_snapshot,
        env_vars=env_vars,
        uv_version=sandbox_cfg.get("uv_version"),
    )

    # Resolve upload mappings — CLI overrides, otherwise auto-upload CWD
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
    else:
        # Auto-upload project root
        upload_mappings = [(str(project_root), remote_project_dir)]

    # Resolve working dir — CLI overrides toml, toml overrides default
    if not working_dir:
        working_dir = sandbox_cfg.get("working_dir", remote_project_dir)

    # Setup commands from toml (run after upload, before agent starts)
    setup_commands: list[str] = sandbox_cfg.get("setup", [])

    return backend, upload_mappings, working_dir, setup_commands


def _apply_compaction_flags(
    target: Any,
    compact_threshold: float | None,
    compact_at_tokens: int | None,
    auto_compact: bool | None,
) -> None:
    """Copy compaction flags the user actually passed onto a config or session."""
    if compact_threshold is not None:
        target.compact_threshold = compact_threshold
    if compact_at_tokens is not None:
        target.compact_at_tokens = compact_at_tokens
    if auto_compact is not None:
        target.auto_compact = auto_compact


def _render_prompt_file(
    system_prompt_file: str, vars_file: str | None, var: list[str] | None
) -> tuple[str, str | None]:
    """Render a --system-prompt file with --vars-file and --var; exits on error."""
    from ..prompts import load_prompt, parse_vars, prepare_prompt

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
                _markup(str(PromptLoadError(f"Vars file not found: {vars_path}")), THEME.error)
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

    if var:
        try:
            prompt_vars.update(parse_vars(var))
        except ValueError as exc:
            console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
            raise typer.Exit(1) from exc

    try:
        return prepare_prompt(loaded_prompt, prompt_vars)
    except ValueError as exc:
        console.print(_markup(str(PromptLoadError(str(exc))), THEME.error))
        raise typer.Exit(1) from exc


def _default_prompt(
    capability_profile: Any, working_dir: str, *, is_remote: bool
) -> tuple[str, str | None]:
    """Render the default system prompt for a profile, working dir and backend.

    Remote backends execute on Linux VMs, not the host machine.
    """
    from ..prompts import load_prompt, prepare_prompt

    prompt_path = DEFAULT_PROMPT_FILE if DEFAULT_PROMPT_FILE.exists() else BUILTIN_PROMPT_FILE
    try:
        loaded_prompt = load_prompt(prompt_path)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"Could not load default prompt at {prompt_path}: {exc}") from exc

    sandbox_info = ""
    if is_remote:
        sandbox_info = (
            "Pre-installed tools: python3, pip, node, git, uv.\n"
            "Prefer uv over pip for Python package management.\n"
            "PATH includes ~/.local/bin.\n"
            "\n"
            "Important: uv-managed venvs do not include pip. To install packages:\n"
            "- `uv pip install <pkg>` — install into the project venv\n"
            "- `uv run --with <pkg> <cmd>` — run a command with a temporary dependency\n"
            "- `uv run <cmd>` — run a command using the project venv\n"
            "Always use `uv run` or `uv pip install` instead of plain `pip` or `.venv/bin/python -m pip`."
        )

    prompt_vars: dict[str, str] = {
        "platform": "Linux" if is_remote else platform.system(),
        "home_dir": "/home/daytona" if is_remote else str(Path.home()),
        "working_dir": working_dir,
        "profile_name": capability_profile.name,
        "shell_mode": capability_profile.shell.value,
        "file_write_mode": capability_profile.file_write.value,
        "database_mode": capability_profile.database.value,
        "sandbox_info": sandbox_info,
    }
    return prepare_prompt(loaded_prompt, prompt_vars)


def _default_prompt_for(config: AgentConfig) -> str | None:
    """The default system prompt a config would get, or None if it cannot be built."""
    if not config.working_dir:
        return None
    try:
        text, _ = _default_prompt(
            load_profile(config.profile), config.working_dir, is_remote=config.backend != "local"
        )
    except (ValueError, FileNotFoundError):
        return None
    return text


def _resume_config(
    base: AgentConfig,
    overrides: dict[str, Any],
    *,
    system_prompt: str | None = None,
) -> tuple[AgentConfig, bool]:
    """The config to resume a session with: ``base`` with flag overrides applied.

    The default system prompt names the profile, working directory and
    platform, so it is rendered again when one of those changes. A custom
    prompt is kept as it is; the second return value is True in that case, to
    tell the user it may describe the old settings.
    """
    config = dataclasses.replace(base, **overrides)
    if system_prompt is not None:
        config.system_prompt = system_prompt
        return config, False
    if not any(getattr(config, k) != getattr(base, k) for k in _PROMPT_FIELDS):
        return config, False
    if base.system_prompt and base.system_prompt == _default_prompt_for(base):
        rendered = _default_prompt_for(config)
        if rendered is not None:
            config.system_prompt = rendered
            return config, False
    return config, bool(base.system_prompt)


@app.command()
def main(
    ctx: typer.Context,
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
    ] = os.getenv("OPENAI_MODEL", "gpt-6.1-terra"),
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
    queue_approvals: Annotated[
        bool,
        typer.Option(
            "--queue-approvals",
            help=(
                "Send approval requests to `rho-agent monitor` instead of prompting here. "
                "This is the default when there is no terminal to prompt on."
            ),
        ),
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
            help="Capability profile: 'readonly' (default), 'developer', 'unrestricted', or path to YAML",
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
    dockerfile: Annotated[
        str | None,
        typer.Option(
            "--dockerfile",
            help="Path to Dockerfile for building a custom sandbox image (Daytona backend)",
        ),
    ] = None,
    snapshot: Annotated[
        str | None,
        typer.Option(
            "--snapshot",
            help="Named Daytona snapshot to create sandbox from (faster than --dockerfile)",
        ),
    ] = None,
    shell_mode: Annotated[
        str | None,
        typer.Option(
            "--shell-mode",
            help="Override shell mode: 'restricted' or 'unrestricted'",
        ),
    ] = None,
    compact_threshold: Annotated[
        float | None,
        typer.Option(
            "--compact-threshold",
            min=0.01,
            max=1.0,
            help="Compact history at this fraction of the context window (default 0.7)",
        ),
    ] = None,
    compact_at_tokens: Annotated[
        int | None,
        typer.Option(
            "--compact-at-tokens",
            min=1,
            help="Compact history at this exact input token count (wins over the fraction)",
        ),
    ] = None,
    auto_compact: Annotated[
        bool | None,
        typer.Option(
            "--auto-compact/--no-auto-compact",
            help="Turn automatic history compaction on or off (default on)",
        ),
    ] = None,
) -> None:
    """rho-agent: An agent harness and CLI with readonly and developer modes."""
    # --- Daytona sandbox configuration ---
    # When --backend daytona, read .rho-agent.toml from the project root.
    # CLI flags (--dockerfile, --snapshot, --upload, --working-dir) override the toml.
    daytona_backend = None
    upload_mappings: list[tuple[str, str]] = []
    setup_commands: list[str] = []

    if backend == "daytona" or (dockerfile or snapshot):
        if backend == "local" and (dockerfile or snapshot):
            backend = "daytona"  # --dockerfile/--snapshot implies daytona

        if dockerfile and snapshot:
            console.print(
                _markup("Cannot use both --dockerfile and --snapshot", THEME.error)
            )
            raise typer.Exit(1)

        daytona_backend, upload_mappings, working_dir, setup_commands = _resolve_daytona_config(
            dockerfile=dockerfile,
            snapshot=snapshot,
            upload=upload,
            working_dir=working_dir,
        )
    elif upload:
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

    # Handle --resume: find the session to load
    if resume and resume.lower() == "latest":
        latest_id = session_store.get_latest_id()
        if not latest_id:
            console.print(_markup("No saved sessions to resume.", THEME.error))
            raise typer.Exit(1)
        resume = latest_id

    # Resolve working directory — skip local resolution for remote backends
    # (macOS resolves /home → /System/Volumes/Data/home which breaks remote paths)
    if working_dir and backend != "local":
        resolved_working_dir = working_dir
    elif working_dir:
        resolved_working_dir = str(Path(working_dir).expanduser().resolve())
    else:
        resolved_working_dir = os.getcwd()

    initial_prompt: str | None = None

    # Build AgentConfig from the saved session, a --config file, or flags
    if resume:
        # The saved config (or --config) is the base; flags given on the
        # command line override it for this run. config.yaml is not rewritten.
        try:
            base_config = session_store.load_config(resume)
        except FileNotFoundError as err:
            console.print(_markup(f"Session not found: {resume}", THEME.error))
            console.print(_markup("Use --list to see saved sessions.", THEME.muted))
            raise typer.Exit(1) from err
        if config_file:
            base_config = AgentConfig.from_file(config_file)

        def given(name: str) -> bool:
            # Compared by name: the enum lives in click, or inside typer in
            # releases that bundle their own copy of click.
            source = ctx.get_parameter_source(name)
            return getattr(source, "name", None) == "COMMANDLINE"

        overrides: dict[str, Any] = {}
        if given("model"):
            overrides["model"] = model
        if given("base_url"):
            overrides["base_url"] = base_url
        if given("reasoning_effort"):
            overrides["reasoning_effort"] = reasoning_effort
        if given("profile"):
            overrides["profile"] = profile
        if daytona_backend or given("backend"):
            overrides["backend"] = daytona_backend or backend
        if given("working_dir") or (working_dir and "backend" in overrides):
            overrides["working_dir"] = resolved_working_dir

        new_prompt = None
        if system_prompt_file:
            new_prompt, _ = _render_prompt_file(system_prompt_file, vars_file, var)
        try:
            agent_config, prompt_kept = _resume_config(
                base_config, overrides, system_prompt=new_prompt
            )
            session = session_store.resume(resume, agent_config=agent_config)
        except (ValueError, FileNotFoundError) as e:
            console.print(_markup(str(InvalidProfileError(str(e))), THEME.error))
            raise typer.Exit(1) from e
        agent = session.agent
        if prompt_kept:
            console.print(
                _markup(
                    "Keeping the session's custom system prompt; it may describe the "
                    "previous profile, working directory or backend.",
                    THEME.warning,
                )
            )
        if shell_mode:
            console.print(_markup("--shell-mode is ignored with --resume.", THEME.warning))

        capability_profile = load_profile(agent.config.profile)
        mode_name = capability_profile.name
        resolved_working_dir = agent.config.working_dir or resolved_working_dir
        # Flags apply to the resumed session and to any child agents it starts.
        for target in (agent.config, session):
            _apply_compaction_flags(target, compact_threshold, compact_at_tokens, auto_compact)
        console.print(_markup(f"Resuming session: {session.id}", THEME.success))
    elif config_file:
        # Load from config file with CLI overrides
        agent_config = AgentConfig.from_file(config_file)
        if model != os.getenv("OPENAI_MODEL", "gpt-6.1-terra"):
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
        _apply_compaction_flags(agent_config, compact_threshold, compact_at_tokens, auto_compact)

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
            except ValueError as err:
                console.print(
                    _markup(
                        str(InvalidModeError("shell mode", shell_mode, "restricted, unrestricted")),
                        THEME.error,
                    )
                )
                raise typer.Exit(1) from err

        # Resolve system prompt
        if system_prompt_file:
            system_prompt_text, initial_prompt = _render_prompt_file(
                system_prompt_file, vars_file, var
            )
        else:
            try:
                system_prompt_text, initial_prompt = _default_prompt(
                    capability_profile, resolved_working_dir, is_remote=backend != "local"
                )
            except (FileNotFoundError, ValueError) as exc:
                console.print(_markup(f"Prompt error: {PromptLoadError(str(exc))}", THEME.error))
                raise typer.Exit(1) from exc

        # Load global databases.yaml for CLI users (SDK users set databases on AgentConfig directly)
        from ..tools.handlers.database_config import load_database_config_raw

        cli_databases = load_database_config_raw()

        agent_config = AgentConfig(
            system_prompt=system_prompt_text,
            model=model,
            profile=effective_profile,
            backend=daytona_backend or backend,
            base_url=base_url,
            reasoning_effort=reasoning_effort,
            working_dir=resolved_working_dir,
            databases=cli_databases,
            auto_approve=auto_approve,
        )
        _apply_compaction_flags(agent_config, compact_threshold, compact_at_tokens, auto_compact)
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
    else:
        agent.registry.unregister("delegate")

    if not agent.config.base_url and not os.getenv("OPENAI_API_KEY"):
        console.print(_markup(str(MissingApiKeyError()), THEME.error))
        raise typer.Exit(1)

    # Determine the prompt to run
    run_prompt_text = (
        prompt
        if prompt
        else (prompt_arg if prompt_arg else (initial_prompt if not resume else None))
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
    if queue_approvals or not sys.stdin.isatty():
        # Nobody can answer a prompt here; the monitor answers instead.
        approval_handler.queue = ApprovalQueue(session, on_request=announce_queued_approval)
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
                    setup_commands=setup_commands,
                )
            )
        else:
            asyncio.run(
                run_single(
                    session,
                    run_prompt_text,
                    upload_mappings=upload_mappings,
                    setup_commands=setup_commands,
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
                setup_commands=setup_commands,
            )
        )


def _register_delegate(agent: Agent, session: Session) -> None:
    """Make sure the delegate tool is present for profiles that allow it.

    The tool factory registers it already; this covers registries that were
    replaced after the agent was built. The handler reads its context from the
    calling session, so nothing session-specific is bound here.
    """
    from ..permissions.factory import ToolFactory, load_profile

    if "delegate" in agent.registry:
        return
    try:
        profile = load_profile(agent.config.profile)
    except (ValueError, FileNotFoundError):
        return

    if not hasattr(profile, "requires_tool_approval") or profile.bash_only:
        return

    ToolFactory(profile).register_delegate_tool(agent.registry)
