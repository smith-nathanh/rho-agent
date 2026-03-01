"""Main session loop for continuum."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from ..core.agent import Agent
from ..core.session import Session
from ..prompts import load_prompt, prepare_prompt
from .checks import run_checks
from .git_ops import create_branch, git_add_and_commit
from .handoffs import (
    ensure_handoffs_dir,
    latest_handoff,
    latest_handoff_number,
    write_handoff,
)
from .models import ContinuumConfig, ContinuumState, SessionUsage
from .state import latest_state_path, load_state, save_state, state_path_for_run

console = Console()

BUDGET_WARNING = (
    "Your context budget is getting full. Begin wrapping up your current work — "
    "finish what you're in the middle of, commit your changes, then write your "
    "handoff document per the instructions in your system prompt."
)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "agent.md"

_PROJECT_COMPLETE_RE = re.compile(r"^PROJECT COMPLETE$", re.MULTILINE)
_NEEDS_INPUT_RE = re.compile(r"^NEEDS_INPUT$", re.MULTILINE)


def _parse_handoff(text: str) -> tuple[str, str] | None:
    """Extract slug and content from agent response containing HANDOFF: slug."""
    match = re.search(r"HANDOFF:\s*(\S+)", text)
    if not match:
        return None
    slug = match.group(1).strip().lower()
    content = text[match.end():].strip()
    return slug, content


def _format_verification_commands(config: ContinuumConfig) -> str:
    """Build a human-readable list of verification commands."""
    lines = []
    if config.test_cmd:
        lines.append(f"- Test: `{config.test_cmd}`")
    if config.lint_cmd:
        lines.append(f"- Lint: `{config.lint_cmd}`")
    if config.typecheck_cmd:
        lines.append(f"- Typecheck: `{config.typecheck_cmd}`")
    return "\n".join(lines)


async def run_continuum(config: ContinuumConfig) -> ContinuumState:
    """Run the continuum session loop."""
    run_id = str(uuid.uuid4())[:12]
    requested_state_path = (
        Path(config.state_path).expanduser().resolve() if config.state_path else None
    )
    sp = requested_state_path if requested_state_path else state_path_for_run(run_id)

    # Load or create state
    if config.resume:
        if requested_state_path is None:
            found = latest_state_path()
            if found is None:
                raise FileNotFoundError(
                    "No saved continuum state found to resume. "
                    "Provide --state or run without --resume."
                )
            sp = found
        if not sp.exists():
            raise FileNotFoundError(f"State file not found: {sp}")
        state = load_state(sp)
        run_id = state.run_id
        console.print(f"[green]Resumed continuum run {run_id}[/green]")
    else:
        state = ContinuumState(run_id=run_id, config=config)

    try:
        # Load PRD
        prd_path = Path(config.prd_path).expanduser().resolve()
        prd_text = prd_path.read_text(encoding="utf-8")

        # Ensure handoffs directory exists
        ensure_handoffs_dir(config.working_dir)

        # Optionally create git branch
        if config.git_branch:
            try:
                await create_branch(config.working_dir, config.git_branch)
                console.print(f"[green]Created branch: {config.git_branch}[/green]")
            except RuntimeError:
                console.print(
                    f"[yellow]Branch '{config.git_branch}' may already exist, "
                    f"continuing on current branch[/yellow]"
                )

        # Load prompt template once
        prompt_template = load_prompt(_PROMPT_PATH)
        verification_commands = _format_verification_commands(config)

        # Session loop
        for session_num in range(1, config.max_sessions + 1):
            console.print(
                f"\n[bold]{'=' * 60}[/bold]"
                f"\n[bold]Session {session_num}/{config.max_sessions}[/bold]"
            )

            # Read latest handoff
            handoff_doc = latest_handoff(config.working_dir) or ""

            # Prepare prompt
            system_prompt, initial_prompt = prepare_prompt(prompt_template, {
                "prd_text": prd_text,
                "handoff_doc": handoff_doc,
                "verification_commands": verification_commands,
                "working_dir": config.working_dir,
            })

            # Create agent and session
            agent_cfg = config.agent_config(system_prompt)
            agent = Agent(agent_cfg)
            session = Session(agent)
            session.context_window = config.context_window
            # Disable auto-compaction — context lifecycle is managed via
            # handoffs and the budget gate rather than in-place summarization.
            session.auto_compact = False

            def _make_budget_gate(cfg: ContinuumConfig):
                def gate(input_tokens: int) -> str | None:
                    if input_tokens >= cfg.context_window * cfg.budget_threshold:
                        return BUDGET_WARNING
                    return None
                return gate

            session.budget_gate = _make_budget_gate(config)

            try:
                # Run the agent (with NEEDS_INPUT loop)
                console.print("  [dim]Agent working...[/dim]")
                prompt = initial_prompt
                while True:
                    result = await session.run(prompt)
                    response_text = result.text or ""

                    # Check for NEEDS_INPUT before PROJECT COMPLETE
                    if _NEEDS_INPUT_RE.search(response_text):
                        console.print(f"\n{response_text}\n")
                        user_response = Prompt.ask(
                            "[bold]Agent needs your input[/bold]"
                        )
                        prompt = user_response
                        console.print("  [dim]Agent working...[/dim]")
                        continue

                    # No more NEEDS_INPUT — exit the inner loop
                    break

                # Check for project completion
                if _PROJECT_COMPLETE_RE.search(response_text):
                    console.print("  [bold green]Agent signaled PROJECT COMPLETE[/bold green]")
                    # Run final checks
                    check_result = await run_checks(config.verification, config.working_dir)
                    if check_result.passed:
                        # Commit any remaining work
                        sha = await git_add_and_commit(
                            config.working_dir, "continuum: final commit"
                        )
                        if sha:
                            console.print(f"  [green]Final commit: {sha[:8]}[/green]")
                        state.status = "completed"
                        state.session_count = session_num
                        _accumulate_usage(state, session)
                        save_state(sp, state)
                        break
                    else:
                        console.print(
                            "  [yellow]Checks failed after PROJECT COMPLETE — "
                            "continuing with handoff[/yellow]"
                        )

                # Commit any uncommitted work
                sha = await git_add_and_commit(
                    config.working_dir,
                    f"continuum: session {session_num}",
                )
                if sha:
                    console.print(f"  [green]Committed: {sha[:8]}[/green]")

                # Parse and write handoff
                parsed = _parse_handoff(response_text)
                handoff_number = latest_handoff_number(config.working_dir) + 1
                if parsed:
                    slug, content = parsed
                    path = write_handoff(config.working_dir, handoff_number, slug, content)
                    console.print(f"  [green]Handoff written: {path.name}[/green]")
                else:
                    # Agent didn't format a proper handoff — save the raw response
                    slug = f"session-{session_num}"
                    content = response_text
                    path = write_handoff(config.working_dir, handoff_number, slug, content)
                    console.print(
                        f"  [yellow]No HANDOFF signal found, saved raw response: {path.name}[/yellow]"
                    )

                state.last_handoff_number = handoff_number
                _accumulate_usage(state, session)
                state.session_count = session_num
                save_state(sp, state)
            finally:
                await session.close()
        else:
            # Exhausted max_sessions without completion
            if state.status == "running":
                state.status = "paused"
                save_state(sp, state)
                console.print(
                    f"\n[yellow]Reached max sessions ({config.max_sessions}). "
                    f"Run with --resume to continue.[/yellow]"
                )

    except Exception as exc:
        state.status = "error"
        save_state(sp, state)
        console.print(f"[red]Continuum error: {exc}[/red]")
        raise
    finally:
        save_state(sp, state)

    _print_summary(state, sp)
    return state


def _accumulate_usage(state: ContinuumState, session: Session) -> None:
    """Add session usage to state totals."""
    usage = session.state.usage
    state.total_usage.input_tokens += usage.get("input_tokens", 0)
    state.total_usage.output_tokens += usage.get("output_tokens", 0)
    state.total_usage.cost_usd += usage.get("cost_usd", 0.0)


def _print_summary(state: ContinuumState, state_path: Path) -> None:
    """Print run summary."""
    u = state.total_usage
    console.print(f"\n[bold]Continuum Summary[/bold]")
    console.print(f"  Sessions: {state.session_count}")
    console.print(f"  Tokens:   {u.input_tokens:,} in / {u.output_tokens:,} out")
    console.print(f"  Cost:     ${u.cost_usd:.4f}")
    console.print(f"  Status:   {state.status}")
    console.print(f"  State:    {state_path}")
