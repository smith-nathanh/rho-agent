"""Sequential conductor loop orchestrating planner, worker, checks, and reviewer."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .checks import run_checks
from .git_ops import (
    create_branch,
    get_head_sha,
    git_add_and_commit,
    git_diff_since,
    is_worktree_clean,
)
from .models import (
    ConductorConfig,
    ConductorState,
    TaskDAG,
    TaskStatus,
    TaskUsage,
)
from .planner import run_planner
from .reviewer import run_reviewer
from .state import load_state, save_state, state_path_for_run, latest_state_path
from .worker import run_worker, run_worker_retry

console = Console()


async def _wait_while_paused(session_dir: Path) -> bool:
    """Block while pause sentinel exists; return False if cancel sentinel appears."""
    announced = False
    while (session_dir / "pause").exists():
        if (session_dir / "cancel").exists():
            return False
        if not announced:
            console.print("[yellow]Conductor paused; waiting for resume...[/yellow]")
            announced = True
        await asyncio.sleep(0.5)
    if announced:
        console.print("[green]Conductor resumed.[/green]")
    return True


def _print_summary(state: ConductorState) -> None:
    """Print a summary table of task results and usage."""
    if not state.dag:
        return

    table = Table(title=f"Conductor Summary — {state.dag.project_name}")
    table.add_column("Task", style="bold")
    table.add_column("Status")
    table.add_column("Attempts", justify="right")
    table.add_column("Commit")
    table.add_column("Worker $", justify="right")
    table.add_column("Reviewer $", justify="right")

    total_cost = 0.0
    for task_id in sorted(state.dag.tasks):
        task = state.dag.tasks[task_id]
        usage = state.usage.get(task_id)
        status_style = {
            TaskStatus.DONE: "green",
            TaskStatus.FAILED: "red",
            TaskStatus.PENDING: "dim",
            TaskStatus.IN_PROGRESS: "yellow",
        }.get(task.status, "")
        worker_cost = f"${usage.worker_cost_usd:.4f}" if usage else "-"
        reviewer_cost = f"${usage.reviewer_cost_usd:.4f}" if usage else "-"
        if usage:
            total_cost += usage.worker_cost_usd + usage.reviewer_cost_usd
        table.add_row(
            f"{task.id}: {task.title}",
            f"[{status_style}]{task.status.value}[/{status_style}]",
            str(task.attempts),
            (task.review_sha or task.commit_sha or "-")[:8],
            worker_cost,
            reviewer_cost,
        )

    console.print(table)
    console.print(f"[bold]Total cost:[/bold] ${total_cost:.4f}")
    console.print(f"[bold]Final status:[/bold] {state.status}")


async def run_conductor(config: ConductorConfig) -> ConductorState:
    """Run the sequential conductor loop."""
    run_id = str(uuid.uuid4())[:12]
    requested_state_path = (
        Path(config.state_path).expanduser().resolve() if config.state_path else None
    )
    sp = requested_state_path if requested_state_path else state_path_for_run(run_id)

    # Create a session directory for sentinel-based control
    session_dir = sp.parent / f"conductor-{run_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # Load or create state
    if config.resume:
        if requested_state_path is None:
            found = latest_state_path()
            if found is None:
                raise FileNotFoundError(
                    "No saved conductor state found to resume. "
                    "Provide --state or run without --resume."
                )
            sp = found
        if not sp.exists():
            raise FileNotFoundError(f"State file not found: {sp}")
        state = load_state(sp)
        run_id = state.run_id
        session_dir = sp.parent / f"conductor-{run_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Resumed conductor run {run_id}[/green]")
    else:
        state = ConductorState(run_id=run_id, config=config)

    def _cancel_check() -> bool:
        return (session_dir / "cancel").exists()

    try:
        # Load PRD text (needed for both planning and worker context)
        prd_path = Path(config.prd_path).expanduser().resolve()
        prd_text = prd_path.read_text(encoding="utf-8")

        # Planning phase
        if state.dag is None:
            console.print(f"[bold]Planning tasks from:[/bold] {prd_path.name}")

            dag, planner_usage = await run_planner(prd_text, config, cancel_check=_cancel_check)
            state.dag = dag
            save_state(sp, state)
            console.print(
                f"[green]Planned {len(dag.tasks)} tasks for project '{dag.project_name}'[/green]"
            )
            for t in sorted(dag.tasks.values(), key=lambda t: t.id):
                deps = f" (depends: {', '.join(t.depends_on)})" if t.depends_on else ""
                console.print(f"  {t.id}: {t.title}{deps}")

        dag = state.dag
        assert dag is not None

        # Optionally create a git branch
        if config.git_branch:
            try:
                await create_branch(config.working_dir, config.git_branch)
                console.print(f"[green]Created branch: {config.git_branch}[/green]")
            except RuntimeError:
                console.print(
                    f"[yellow]Branch '{config.git_branch}' may already exist, "
                    f"continuing on current branch[/yellow]"
                )

        # Sequential task loop
        while True:
            # Check signals
            if (session_dir / "cancel").exists():
                console.print("[red]Conductor cancelled.[/red]")
                state.status = "cancelled"
                break

            if not await _wait_while_paused(session_dir):
                state.status = "cancelled"
                break

            if not await is_worktree_clean(config.working_dir):
                state.status = "error"
                console.print(
                    "[red]Working tree is not clean. Conductor requires a clean "
                    "tree between tasks to preserve per-task commit isolation.[/red]"
                )
                break

            task = dag.next_ready_task()
            if task is None:
                if dag.all_done():
                    state.status = "completed"
                    console.print("[bold green]All tasks completed![/bold green]")
                else:
                    state.status = "failed"
                    failed = [t for t in dag.tasks.values() if t.status == TaskStatus.FAILED]
                    blocked = [t for t in dag.tasks.values() if t.status == TaskStatus.PENDING]
                    console.print(
                        f"[red]No ready tasks. {len(failed)} failed, {len(blocked)} blocked.[/red]"
                    )
                break

            # Work on this task
            task.status = TaskStatus.IN_PROGRESS
            task.attempts += 1
            save_state(sp, state)

            usage = state.usage.setdefault(task.id, TaskUsage(task_id=task.id))
            base_sha = await get_head_sha(config.working_dir)
            console.print(
                f"\n[bold]{'=' * 60}[/bold]"
                f"\n[bold]Task {task.id}: {task.title} "
                f"(attempt {task.attempts})[/bold]"
            )

            # --- Worker phase with handoff loop ---
            handoff_doc = task.handoff_doc
            worker_completed = False
            for session_num in range(1, config.max_worker_sessions + 1):
                if (session_dir / "cancel").exists():
                    break

                console.print(
                    f"  [dim]Worker session {session_num}"
                    f"{'  (resuming from handoff)' if handoff_doc else ''}[/dim]"
                )
                worker_result = await run_worker(
                    task,
                    dag,
                    config=config,
                    prd_text=prd_text,
                    handoff_doc=handoff_doc,
                    cancel_check=_cancel_check,
                )

                # Accumulate usage
                usage.worker_input_tokens += worker_result.usage.input_tokens
                usage.worker_output_tokens += worker_result.usage.output_tokens
                usage.worker_cost_usd += worker_result.usage.cost_usd
                usage.worker_sessions += 1

                if worker_result.status == "completed":
                    worker_completed = True
                    break
                elif worker_result.status == "handoff":
                    handoff_doc = worker_result.handoff_doc
                    task.handoff_doc = handoff_doc
                    save_state(sp, state)
                    console.print("  [yellow]Worker handed off to fresh session[/yellow]")
                else:
                    # error or unexpected
                    break

            if (session_dir / "cancel").exists():
                save_state(sp, state)
                continue

            if not worker_completed:
                task.status = TaskStatus.FAILED
                task.error = (
                    "Worker did not complete task within "
                    f"{config.max_worker_sessions} sessions/handoffs. "
                    "Manual intervention required."
                )
                state.status = "paused_user_attention"
                save_state(sp, state)
                console.print(
                    f"  [bold red]Task {task.id} requires attention:[/bold red] "
                    f"not completed after {config.max_worker_sessions} "
                    "sessions/handoffs."
                )
                console.print(
                    "  [bold yellow]Conductor paused. Please inspect/fix the task, "
                    "then resume with --resume.[/bold yellow]"
                )
                break

            # Commit worker changes
            commit_sha = await git_add_and_commit(
                config.working_dir,
                f"conductor: {task.id} - {task.title}",
            )
            if commit_sha:
                task.commit_sha = commit_sha
                console.print(f"  [green]Committed: {commit_sha[:8]}[/green]")
            else:
                console.print("  [dim]No changes to commit[/dim]")

            save_state(sp, state)

            # --- Gate 1: Automated checks ---
            check_result = await run_checks(dag.verification, config.working_dir)
            if not check_result.passed:
                console.print("  [red]Checks failed[/red]")

                # Retry loop
                while task.attempts < config.max_task_attempts:
                    task.attempts += 1
                    console.print(f"  [yellow]Retrying (attempt {task.attempts})...[/yellow]")
                    retry_result = await run_worker_retry(
                        task,
                        dag,
                        check_result.output,
                        config=config,
                        cancel_check=_cancel_check,
                    )
                    usage.worker_input_tokens += retry_result.usage.input_tokens
                    usage.worker_output_tokens += retry_result.usage.output_tokens
                    usage.worker_cost_usd += retry_result.usage.cost_usd
                    usage.worker_sessions += 1
                    if retry_result.status != "completed":
                        task.status = TaskStatus.FAILED
                        task.error = "Retry worker did not signal completion with 'TASK COMPLETE'"
                        save_state(sp, state)
                        console.print(
                            f"  [red]Task {task.id} FAILED: retry worker did not complete[/red]"
                        )
                        break

                    retry_sha = await git_add_and_commit(
                        config.working_dir,
                        f"conductor: {task.id} - fix checks (attempt {task.attempts})",
                    )
                    if retry_sha:
                        task.commit_sha = retry_sha

                    check_result = await run_checks(dag.verification, config.working_dir)
                    if check_result.passed:
                        console.print("  [green]Checks passed after retry[/green]")
                        break

                if task.status == TaskStatus.FAILED:
                    continue

                if not check_result.passed:
                    task.status = TaskStatus.FAILED
                    task.error = check_result.output[:2000]
                    save_state(sp, state)
                    console.print(
                        f"  [red]Task {task.id} FAILED after {task.attempts} attempts[/red]"
                    )
                    continue
            else:
                console.print("  [green]Checks passed[/green]")

            # --- Gate 2: Reviewer ---
            if config.enable_reviewer and task.commit_sha:
                diff_text = await git_diff_since(config.working_dir, base_sha)
                if diff_text.strip():
                    console.print("  [dim]Running reviewer...[/dim]")
                    review_result = await run_reviewer(
                        task,
                        dag,
                        diff_text,
                        config=config,
                        cancel_check=_cancel_check,
                    )
                    usage.reviewer_input_tokens += review_result.usage.input_tokens
                    usage.reviewer_output_tokens += review_result.usage.output_tokens
                    usage.reviewer_cost_usd += review_result.usage.cost_usd

                    # Commit any reviewer fixes
                    review_sha = await git_add_and_commit(
                        config.working_dir,
                        f"conductor: {task.id} - reviewer fixes",
                    )
                    if review_sha:
                        task.review_sha = review_sha
                        console.print(
                            f"  [green]Reviewer committed fixes: {review_sha[:8]}[/green]"
                        )

                    # Re-check after reviewer fixes
                    post_review_check = await run_checks(dag.verification, config.working_dir)
                    if not post_review_check.passed:
                        task.status = TaskStatus.FAILED
                        task.error = post_review_check.output[:2000]
                        save_state(sp, state)
                        console.print(
                            f"  [red]Task {task.id} FAILED: checks failed after reviewer[/red]"
                        )
                        continue

            # Task done
            task.status = TaskStatus.DONE
            save_state(sp, state)
            console.print(f"  [bold green]Task {task.id} DONE[/bold green]")
            if task.attempts > 1:
                console.print(
                    f"  [bold yellow]Retry alert:[/bold yellow] "
                    f"Task {task.id} required {task.attempts} attempts."
                )

    except Exception as exc:
        state.status = "error"
        save_state(sp, state)
        console.print(f"[red]Conductor error: {exc}[/red]")
        raise
    finally:
        save_state(sp, state)

    _print_summary(state)
    console.print(f"[dim]State saved to: {sp}[/dim]")
    return state
