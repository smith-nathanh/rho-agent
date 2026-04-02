"""Core evolution loop."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.session import Session
from ..tools.base import ToolHandler
from .archive import append_generation, best_generation, load_archive, select_parent
from .harness import DomainHarness, load_harness
from .models import EvolveConfig, Generation
from .workspace import (
    build_agent_from_workspace,
    copy_workspace,
    create_workspace,
    load_prompt_from_workspace,
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


def _gen_id(generation: int) -> str:
    short = uuid.uuid4().hex[:6]
    return f"gen-{generation:04d}-{short}"


def _workspace_inventory(workspace: Path) -> str:
    lines = []
    for p in sorted(workspace.rglob("*")):
        if p.is_file():
            rel = p.relative_to(workspace)
            lines.append(f"  {rel}")
    return "\n".join(lines) if lines else "  (empty)"


def _render_meta_prompt(
    *,
    generation: int,
    parent_score: float | None,
    best_score: float | None,
    parent_feedback: str,
    workspace: Path,
    harness: DomainHarness,
    scenario_sample: list[dict[str, Any]],
) -> str:
    from ..prompts.renderer import render_string

    return render_string(
        _META_PROMPT_TEMPLATE,
        {
            "generation": generation,
            "parent_score": parent_score if parent_score is not None else "N/A",
            "best_score": best_score if best_score is not None else "N/A",
            "parent_feedback": parent_feedback,
            "workspace_inventory": _workspace_inventory(workspace),
            "tool_handler_api": _TOOL_HANDLER_SOURCE,
            "domain_description": harness.__class__.__doc__ or "No domain description.",
            "scenario_sample": json.dumps(scenario_sample, indent=2, default=str),
        },
    )


async def _run_eval(
    harness: DomainHarness,
    agent: Agent,
    scenarios: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    """Evaluate an agent on a list of scenarios, return (score, results)."""
    results = []
    for scenario in scenarios:
        try:
            result = await harness.run_agent(agent, scenario)
            results.append(result)
        except Exception as e:
            results.append({
                "scenario_id": scenario.get("id", "unknown"),
                "success": False,
                "error": str(e),
            })
    score = harness.score(results)
    return score, results


async def run_evolve(config: EvolveConfig) -> list[Generation]:
    """Run the evolutionary loop.

    Returns the full list of generations from the archive.
    """
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    archive_path = run_dir / "archive.jsonl"

    harness = load_harness(config.harness, **config.harness_kwargs)
    all_scenarios = harness.scenarios()

    # --- Gen 0: seed workspace ---
    gen0_id = _gen_id(0)
    gen0_workspace = create_workspace(config.run_dir, gen0_id)

    if config.seed_workspace:
        copy_workspace(Path(config.seed_workspace), gen0_workspace)

    gen0 = Generation(
        gen_id=gen0_id,
        generation=0,
        parent_id=None,
        workspace_path=str(gen0_workspace),
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Evaluate gen 0
    try:
        agent = build_agent_from_workspace(gen0_workspace, config)
        gen0.status = "evaluating"
        score, results = await _run_eval(harness, agent, all_scenarios)
        gen0.score = score
        gen0.status = "scored"
        # Write results for next generation's meta-agent
        (gen0_workspace / "eval_results.json").write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8"
        )
    except Exception as e:
        gen0.status = "error"
        gen0.error = str(e)
        logger.exception("Gen 0 evaluation failed")

    append_generation(archive_path, gen0)
    logger.info("Gen 0 [%s]: score=%s status=%s", gen0_id, gen0.score, gen0.status)

    # --- Generations 1..N ---
    for gen_num in range(1, config.max_generations):
        parent = select_parent(archive_path)
        if parent is None:
            logger.warning("No scored parent available, stopping.")
            break

        gen_id = _gen_id(gen_num)
        workspace = create_workspace(config.run_dir, gen_id)
        copy_workspace(Path(parent.workspace_path), workspace)

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
            best_score = current_best.score if current_best else None

            # Get feedback from parent's results
            parent_results_path = Path(parent.workspace_path) / "eval_results.json"
            parent_feedback = ""
            if parent_results_path.exists():
                parent_results = json.loads(
                    parent_results_path.read_text(encoding="utf-8")
                )
                parent_feedback = harness.feedback(parent_results)

            meta_prompt = _render_meta_prompt(
                generation=gen_num,
                parent_score=parent.score,
                best_score=best_score,
                parent_feedback=parent_feedback,
                workspace=workspace,
                harness=harness,
                scenario_sample=harness.staged_sample(config.staged_sample_n),
            )

            meta_config = AgentConfig(
                system_prompt=meta_prompt,
                model=config.model,
                profile="unrestricted",
                working_dir=str(workspace),
                auto_approve=True,
            )
            meta_agent = Agent(meta_config)
            async with Session(meta_agent) as session:
                result = await session.run(
                    "Improve the task-agent. Read the workspace first, then make one targeted improvement.",
                    max_turns=30,
                )
                gen.meta_usage = result.usage

        except Exception as e:
            gen.status = "error"
            gen.error = f"Meta-agent failed: {e}"
            append_generation(archive_path, gen)
            logger.exception("Gen %d meta-agent failed", gen_num)
            continue

        # --- Staged eval (quick filter) ---
        try:
            task_agent = build_agent_from_workspace(workspace, config)
            staged_scenarios = harness.staged_sample(config.staged_sample_n)
            staged_score, _ = await _run_eval(harness, task_agent, staged_scenarios)
            gen.staged_score = staged_score

            # Filter: skip full eval if staged score is much worse than parent
            if parent.score is not None and staged_score < parent.score * 0.5:
                gen.status = "filtered"
                append_generation(archive_path, gen)
                logger.info(
                    "Gen %d [%s]: filtered (staged=%.3f < parent=%.3f * 0.5)",
                    gen_num, gen_id, staged_score, parent.score,
                )
                continue
        except Exception as e:
            gen.status = "error"
            gen.error = f"Staged eval failed: {e}"
            append_generation(archive_path, gen)
            logger.exception("Gen %d staged eval failed", gen_num)
            continue

        # --- Full eval ---
        try:
            gen.status = "evaluating"
            score, results = await _run_eval(harness, task_agent, all_scenarios)
            gen.score = score
            gen.status = "scored"
            (workspace / "eval_results.json").write_text(
                json.dumps(results, indent=2, default=str), encoding="utf-8"
            )
        except Exception as e:
            gen.status = "error"
            gen.error = f"Full eval failed: {e}"
            logger.exception("Gen %d full eval failed", gen_num)

        append_generation(archive_path, gen)
        logger.info("Gen %d [%s]: score=%s status=%s", gen_num, gen_id, gen.score, gen.status)

    return load_archive(archive_path)
