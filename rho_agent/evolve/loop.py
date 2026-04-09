"""Core evolution loop."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.session import Session
from ..tools.base import ToolHandler
from .archive import (
    append_generation,
    best_generation,
    load_archive,
    mark_invalid_parent,
    select_parent,
)
from .harness import DomainHarness, load_harness
from .models import EvolveConfig, Generation
from .workspace import (
    build_agent_from_workspace,
    commit_pre_mutation,
    create_workspace,
    extract_diff,
    get_lineage,
    load_prompt_from_workspace,
    load_tools_from_workspace,
    materialize_workspace,
)

logger = logging.getLogger(__name__)

# Read the ToolHandler source once for the meta-agent prompt
_TOOL_HANDLER_SOURCE = ""
try:
    _tool_handler_file = Path(__file__).parent.parent / "tools" / "base.py"
    _TOOL_HANDLER_SOURCE = _tool_handler_file.read_text(encoding="utf-8")
except Exception:
    _TOOL_HANDLER_SOURCE = "# Could not load ToolHandler source"

_META_PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "meta_agent.md").read_text(
    encoding="utf-8"
)

_METACOGNITIVE_PREAMBLE = (
    Path(__file__).parent / "prompts" / "metacognitive_preamble.md"
).read_text(encoding="utf-8")


def _gen_id(generation: int) -> str:
    short = uuid.uuid4().hex[:6]
    return f"gen-{generation:04d}-{short}"


def _workspace_inventory(workspace: Path) -> str:
    lines = []
    for p in sorted(workspace.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(workspace)
            lines.append(f"  {rel}")
    return "\n".join(lines) if lines else "  (empty)"


def _build_lineage_summary(gen_id: str, archive: list[Generation]) -> str:
    """Build a concise mutation history from the lineage.

    Reads each ancestor's mutation_note.txt and score to produce a summary
    the meta-agent can use to avoid repeating failed strategies.
    """
    chain = get_lineage(gen_id, archive)
    if len(chain) <= 1:
        return ""

    lines = []
    for i, gen in enumerate(chain):
        score_str = f"{gen.score:.2f}" if gen.score is not None else "N/A"
        status_tag = ""
        if gen.status == "error":
            status_tag = " [ERROR]"
        elif gen.status == "filtered":
            status_tag = " [FILTERED]"

        # Try to read mutation note
        ws = Path(gen.workspace_path)
        note_file = ws / "mutation_note.txt"
        if note_file.exists():
            note = note_file.read_text(encoding="utf-8").strip().split("\n")[0]  # first line only
        elif i == 0:
            note = "Initial workspace (seed)"
        else:
            note = "(no note left)"

        # Show score delta if we have parent score
        parent_score = None
        if gen.parent_id:
            for ancestor in chain:
                if ancestor.gen_id == gen.parent_id:
                    parent_score = ancestor.score
                    break
        if parent_score is not None and gen.score is not None:
            delta = gen.score - parent_score
            delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
            lines.append(f"- **{gen.gen_id}** (score {score_str}, {delta_str}){status_tag}: {note}")
        else:
            lines.append(f"- **{gen.gen_id}** (score {score_str}){status_tag}: {note}")

    return "\n".join(lines)


def _build_performance_history(archive: list[Generation]) -> dict[str, Any]:
    """Build performance history from the archive for memory/performance_history.json."""
    scored = [g for g in archive if g.score is not None]
    if not scored:
        return {"generations": [], "statistics": {}}

    scores = [g.score for g in scored]
    entries = []
    for g in scored:
        entries.append({
            "gen_id": g.gen_id,
            "generation": g.generation,
            "score": g.score,
            "status": g.status,
            "parent_id": g.parent_id,
            "created_at": g.created_at,
        })

    stats: dict[str, Any] = {
        "total_scored": len(scored),
        "best_score": max(scores),
        "worst_score": min(scores),
        "average_score": sum(scores) / len(scores),
    }

    # Moving-average trend (last 5 vs previous 5)
    if len(scores) >= 4:
        window = min(5, len(scores) // 2)
        recent_avg = sum(scores[-window:]) / window
        older_avg = sum(scores[-window * 2 : -window]) / window
        stats["improvement_trend"] = round(recent_avg - older_avg, 4)

    return {"generations": entries, "statistics": stats}


def _write_trace_summary(trace_dir: Path, results: list[dict[str, Any]]) -> None:
    """Write a summary.md in the traces dir for the meta-agent to read first."""
    if not trace_dir.exists():
        trace_dir.mkdir(parents=True, exist_ok=True)

    lines = ["# Evaluation Trace Summary", ""]
    solved = sum(1 for r in results if r.get("success"))
    lines.append(f"**Overall: {solved}/{len(results)} solved**")
    lines.append("")

    # Successes
    successes = [r for r in results if r.get("success")]
    if successes:
        lines.append("## Solved")
        for r in successes:
            tokens = r.get("tokens_used", "?")
            lines.append(f"- {r.get('scenario_id', '?')} ({tokens} tokens)")
        lines.append("")

    # Failures
    failures = [r for r in results if not r.get("success")]
    if failures:
        lines.append("## Failed")
        for r in failures:
            sid = r.get("scenario_id", "?")
            err = r.get("error") or "tests failed"
            tokens = r.get("tokens_used", "?")
            has_trace = (trace_dir / sid / "trace.jsonl").exists() if sid != "?" else False
            trace_note = f" [trace: traces/{sid}/trace.jsonl]" if has_trace else ""
            lines.append(f"- **{sid}** ({tokens} tokens): {err[:120]}{trace_note}")
        lines.append("")

    (trace_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _render_meta_prompt(
    *,
    generation: int,
    parent_score: float | None,
    best_score: float | None,
    parent_feedback: str,
    lineage_summary: str,
    workspace: Path,
    harness: DomainHarness,
    scenario_sample: list[dict[str, Any]],
) -> str:
    from ..prompts.renderer import render_string

    context = {
        "generation": generation,
        "parent_score": parent_score if parent_score is not None else "N/A",
        "best_score": best_score if best_score is not None else "N/A",
        "parent_feedback": parent_feedback,
        "lineage_summary": lineage_summary,
        "workspace_inventory": _workspace_inventory(workspace),
        "tool_handler_api": _TOOL_HANDLER_SOURCE,
        "domain_description": harness.__class__.__doc__ or "No domain description.",
        "scenario_sample": json.dumps(scenario_sample, indent=2, default=str),
    }

    # Try workspace-local meta_prompt.md first (metacognitive self-modification)
    ws_meta = workspace / "meta_prompt.md"
    if ws_meta.exists():
        try:
            template_str = ws_meta.read_text(encoding="utf-8")
            rendered = render_string(template_str, context)
            return _METACOGNITIVE_PREAMBLE + "\n" + rendered
        except (ValueError, Exception) as e:
            logger.warning(
                "Workspace meta_prompt.md failed to render (%s), using built-in", e
            )

    # Fallback to built-in template
    return _METACOGNITIVE_PREAMBLE + "\n" + render_string(_META_PROMPT_TEMPLATE, context)


def _sanitize_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip ground-truth ``expected`` key from eval results."""
    return [{k: v for k, v in r.items() if k != "expected"} for r in results]


def _cleanup_workspace(workspace: Path) -> None:
    """Remove a materialized workspace to save disk space."""
    if workspace.exists():
        shutil.rmtree(workspace)


_UPLOAD_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv"}


async def _upload_workspace(sandbox: Any, workspace: Path) -> None:
    """Upload local workspace contents into the Daytona sandbox."""
    from daytona import FileUpload

    files = [
        f for f in workspace.rglob("*")
        if f.is_file()
        and not (_UPLOAD_SKIP_DIRS & set(f.relative_to(workspace).parts))
    ]
    if not files:
        return
    remote_root = "/home/daytona/workspace"
    uploads = [
        FileUpload(
            source=str(fp),
            destination=f"{remote_root}/{fp.relative_to(workspace)}",
        )
        for fp in files
    ]
    await sandbox.fs.upload_files(uploads)


def _is_syncable_workspace_relpath(rel: str) -> bool:
    """Return True if a sandbox path is safe to mirror into the local workspace."""
    rel_path = Path(rel)
    if rel_path.is_absolute():
        return False

    parts = rel_path.parts
    if not parts or "." in parts or ".." in parts:
        return False

    return not (_UPLOAD_SKIP_DIRS & set(parts))


async def _download_workspace(sandbox: Any, workspace: Path) -> None:
    """Mirror mutated workspace contents from the Daytona sandbox back to local.

    Downloads all remote files recursively and removes local files that no
    longer exist in the sandboxed workspace so the extracted diff reflects the
    true sandbox mutation.
    """
    remote_root = "/home/daytona/workspace"
    response = await sandbox.process.exec(
        f"cd {remote_root} && find . -type f | sort",
        timeout=30,
    )
    if response.exit_code != 0:
        raise RuntimeError(
            f"Failed to list Daytona workspace files: {response.result or 'unknown error'}"
        )

    remote_rel_paths = {
        line.removeprefix("./")
        for line in (response.result or "").splitlines()
        if line.strip() and _is_syncable_workspace_relpath(line.removeprefix("./"))
    }

    for rel in sorted(remote_rel_paths):
        local_path = workspace / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await sandbox.fs.download_file(f"{remote_root}/{rel}", str(local_path))

    local_files = {
        str(fp.relative_to(workspace))
        for fp in workspace.rglob("*")
        if fp.is_file() and not (_UPLOAD_SKIP_DIRS & set(fp.relative_to(workspace).parts))
    }
    for rel in sorted(local_files - remote_rel_paths):
        (workspace / rel).unlink(missing_ok=True)

    for path in sorted(workspace.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()) and ".git" not in path.parts:
            path.rmdir()


def _validate_workspace(workspace: Path) -> str | None:
    """Check that workspace Python files parse correctly.

    Returns None if valid, or an error message if broken.
    Catches syntax errors in tools/ and lib/ before burning eval runs.
    """
    for subdir in ("tools", "lib"):
        d = workspace / subdir
        if not d.exists():
            continue
        for py_file in d.rglob("*.py"):
            if py_file.name.startswith("_"):
                continue
            try:
                compile(py_file.read_text(encoding="utf-8"), str(py_file), "exec")
            except SyntaxError as e:
                return f"Syntax error in {py_file.relative_to(workspace)}: {e}"
    return None


async def _run_eval(
    harness: DomainHarness,
    agent: Agent,
    scenarios: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    """Evaluate an agent on a list of scenarios, return (score, results)."""
    results = await harness.run_all(agent, scenarios)
    score = harness.score(results)
    return score, results


async def run_evolve(config: EvolveConfig) -> list[Generation]:
    """Run the evolutionary loop.

    Each generation's mutation is stored as a git diff. Workspaces are
    materialized from the lineage diff chain (root -> ... -> parent -> self)
    and cleaned up after use.

    Supports resuming: if archive.jsonl already exists, continues from the
    last completed generation.

    Returns the full list of generations from the archive.
    """
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    archive_path = run_dir / "archive.jsonl"
    diffs_dir = run_dir / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    # Persist run config
    config_path = run_dir / "config.json"
    config_path.write_text(
        json.dumps(config.to_serializable_dict(), indent=2, default=str),
        encoding="utf-8",
    )

    harness = load_harness(config.harness, **config.harness_kwargs)
    await harness.ensure_loaded()
    all_scenarios = harness.scenarios()

    # --- Resume support ---
    existing_archive = load_archive(archive_path)
    if existing_archive:
        start_gen = max(g.generation for g in existing_archive) + 1
        logger.info(
            "Resuming from generation %d (%d existing entries)",
            start_gen, len(existing_archive),
        )
    else:
        start_gen = 0

    # --- Gen 0: seed workspace (skip if resuming) ---
    if start_gen == 0:
        gen0_id = _gen_id(0)
        gen0_workspace = create_workspace(config.run_dir, gen0_id)

        if config.seed_workspace:
            seed = Path(config.seed_workspace)
            for item in seed.iterdir():
                dest = gen0_workspace / item.name
                if item.is_dir():
                    if item.name == ".git":
                        continue
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        # Seed meta_prompt.md if not already present (from seed or transfer)
        meta_prompt_dest = gen0_workspace / "meta_prompt.md"
        if not meta_prompt_dest.exists():
            meta_prompt_dest.write_text(_META_PROMPT_TEMPLATE, encoding="utf-8")

        # Clean up transfer workspace if we materialized one
        if config.transfer_from and config.seed_workspace:
            transfer_ws = Path(config.seed_workspace)
            if transfer_ws.exists() and "_transfer_" in transfer_ws.name:
                _cleanup_workspace(transfer_ws)

        gen0_diff = extract_diff(gen0_workspace, config.run_dir, gen0_id)

        gen0 = Generation(
            gen_id=gen0_id,
            generation=0,
            parent_id=None,
            workspace_path=str(gen0_workspace),
            diff_path=str(gen0_diff),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            agent = build_agent_from_workspace(gen0_workspace, config)
            gen0.status = "evaluating"
            # Set trace dir and workspace for harnesses that support it
            trace_dir = gen0_workspace / "traces"
            harness.set_trace_dir(trace_dir)
            harness.set_workspace(gen0_workspace, config)
            score, results = await _run_eval(harness, agent, all_scenarios)
            gen0.score = score
            gen0.status = "scored"
            (gen0_workspace / "eval_results.json").write_text(
                json.dumps(_sanitize_results(results), indent=2, default=str),
                encoding="utf-8",
            )
            _write_trace_summary(trace_dir, results)
        except Exception as e:
            gen0.status = "error"
            gen0.error = str(e)
            logger.exception("Gen 0 evaluation failed")

        append_generation(archive_path, gen0)
        logger.info("Gen 0 [%s]: score=%s status=%s", gen0_id, gen0.score, gen0.status)
        start_gen = 1

    # --- Generations 1..N ---
    for gen_num in range(start_gen, config.max_generations):
        parent = select_parent(archive_path, strategy=config.parent_strategy)
        if parent is None:
            logger.warning("No valid scored parent available, stopping.")
            break

        gen_id = _gen_id(gen_num)
        archive = load_archive(archive_path)

        # Materialize workspace from parent's lineage diff chain
        workspace = materialize_workspace(config.run_dir, gen_id, archive, parent_id=parent.gen_id)

        # Copy parent's eval_results.json and traces so the meta-agent can read them
        parent_workspace = Path(parent.workspace_path)
        parent_results_file = parent_workspace / "eval_results.json"
        if parent_results_file.exists():
            shutil.copy2(parent_results_file, workspace / "eval_results.json")
        parent_traces = parent_workspace / "traces"
        if parent_traces.exists():
            dest_traces = workspace / "traces"
            if dest_traces.exists():
                shutil.rmtree(dest_traces)
            shutil.copytree(parent_traces, dest_traces)

        gen = Generation(
            gen_id=gen_id,
            generation=gen_num,
            parent_id=parent.gen_id,
            workspace_path=str(workspace),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # --- Run meta-agent to mutate workspace ---
        try:
            current_best = best_generation(archive_path)
            best_score_val = current_best.score if current_best else None

            parent_feedback = ""
            if parent_results_file.exists():
                parent_results = json.loads(
                    parent_results_file.read_text(encoding="utf-8")
                )
                parent_feedback = harness.feedback(parent_results)

            lineage_summary = _build_lineage_summary(parent.gen_id, archive)

            # Populate performance history for the meta-agent
            perf_history = _build_performance_history(archive)
            memory_dir = workspace / "memory"
            memory_dir.mkdir(exist_ok=True)
            (memory_dir / "performance_history.json").write_text(
                json.dumps(perf_history, indent=2, default=str),
                encoding="utf-8",
            )

            meta_prompt = _render_meta_prompt(
                generation=gen_num,
                parent_score=parent.score,
                best_score=best_score_val,
                parent_feedback=parent_feedback,
                lineage_summary=lineage_summary,
                workspace=workspace,
                harness=harness,
                scenario_sample=harness.staged_sample(config.staged_sample_n),
            )

            commit_pre_mutation(workspace)

            # When using Daytona, the workspace is uploaded to a remote path
            _DAYTONA_WORKSPACE = "/home/daytona/workspace"
            meta_working_dir = (
                _DAYTONA_WORKSPACE if config.daytona_backend else str(workspace)
            )
            meta_config = AgentConfig(
                system_prompt=meta_prompt,
                model=config.model,
                profile="unrestricted",
                working_dir=meta_working_dir,
                auto_approve=True,
                backend=config.daytona_backend or "local",
            )
            meta_agent = Agent(meta_config)
            async with Session(meta_agent) as session:
                # Upload workspace to sandbox if using Daytona
                if config.daytona_backend is not None:
                    sandbox = await session.get_sandbox()
                    await _upload_workspace(sandbox, workspace)

                result = await asyncio.wait_for(
                    session.run(
                        "Modify any part of the workspace to improve the agent.",
                        max_turns=30,
                    ),
                    timeout=config.meta_timeout,
                )
                gen.meta_usage = result.usage

                # Download mutated workspace back from sandbox
                if config.daytona_backend is not None:
                    sandbox = await session.get_sandbox()
                    await _download_workspace(sandbox, workspace)

            # Extract the meta-agent's changes as a diff
            diff_path = extract_diff(workspace, config.run_dir, gen_id)
            gen.diff_path = str(diff_path)

        except asyncio.TimeoutError:
            gen.status = "error"
            gen.error = f"Meta-agent timed out after {config.meta_timeout}s"
            mark_invalid_parent(archive_path, parent.gen_id)
            append_generation(archive_path, gen)
            logger.warning("Gen %d meta-agent timed out", gen_num)
            _cleanup_workspace(workspace)
            continue
        except Exception as e:
            gen.status = "error"
            gen.error = f"Meta-agent failed: {e}"
            mark_invalid_parent(archive_path, parent.gen_id)
            append_generation(archive_path, gen)
            logger.exception("Gen %d meta-agent failed", gen_num)
            _cleanup_workspace(workspace)
            continue

        # --- Validity check: verify workspace Python files parse ---
        validation_error = _validate_workspace(workspace)
        if validation_error:
            gen.status = "error"
            gen.error = f"Workspace validation failed: {validation_error}"
            append_generation(archive_path, gen)
            logger.warning("Gen %d [%s]: %s", gen_num, gen_id, validation_error)
            _cleanup_workspace(workspace)
            continue

        # --- Staged eval (quick filter) ---
        try:
            task_agent = build_agent_from_workspace(workspace, config)
            staged_scenarios = harness.staged_sample(config.staged_sample_n)
            staged_score, _ = await _run_eval(harness, task_agent, staged_scenarios)
            gen.staged_score = staged_score

            if parent.score is not None and staged_score < parent.score * 0.5:
                gen.status = "filtered"
                append_generation(archive_path, gen)
                logger.info(
                    "Gen %d [%s]: filtered (staged=%.3f < parent=%.3f * 0.5)",
                    gen_num, gen_id, staged_score, parent.score,
                )
                _cleanup_workspace(workspace)
                continue
        except Exception as e:
            gen.status = "error"
            gen.error = f"Staged eval failed: {e}"
            append_generation(archive_path, gen)
            logger.exception("Gen %d staged eval failed", gen_num)
            _cleanup_workspace(workspace)
            continue

        # --- Full eval ---
        try:
            gen.status = "evaluating"
            # Set trace dir and workspace so harnesses can save traces / build fresh agents
            trace_dir = workspace / "traces"
            harness.set_trace_dir(trace_dir)
            harness.set_workspace(workspace, config)
            score, results = await _run_eval(harness, task_agent, all_scenarios)
            gen.score = score
            gen.status = "scored"
            (workspace / "eval_results.json").write_text(
                json.dumps(_sanitize_results(results), indent=2, default=str),
                encoding="utf-8",
            )
            # Write trace summary for the meta-agent
            _write_trace_summary(trace_dir, results)
        except Exception as e:
            gen.status = "error"
            gen.error = f"Full eval failed: {e}"
            logger.exception("Gen %d full eval failed", gen_num)

        append_generation(archive_path, gen)
        logger.info("Gen %d [%s]: score=%s status=%s", gen_num, gen_id, gen.score, gen.status)

    return load_archive(archive_path)
