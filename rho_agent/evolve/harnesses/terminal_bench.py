"""TerminalBench evolve harness.

Evaluates an agent's ability to solve terminal-based coding tasks from
TerminalBench. Uses Harbor for container lifecycle and grading. The evolved
agent runs in-process with bash commands routed through Docker exec.

Train/staged splits come from terminal-bench-pro (200 tasks).
Test split is a stratified subset of terminal-bench 2.0 (89 tasks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.session import Session
from ...core.state import State
from ..harness import DomainHarness
from ..workspace import build_agent_from_workspace
from .docker_bash import DockerBashHandler

logger = logging.getLogger(__name__)

# TB2 difficulty classifications (from tbench.ai, April 2026)
_TB2_EASY = {
    "cobol-modernization", "fix-git", "overfull-hbox", "prove-plus-comm",
}
_TB2_MEDIUM = {
    "adaptive-rejection-sampler", "break-filter-js-from-html", "build-cython-ext",
    "build-pmars", "build-pov-ray", "caffe-cifar-10", "chess-best-move",
    "code-from-image", "compile-compcert", "constraints-scheduling",
    "count-dataset-tokens", "crack-7z-hash", "custom-memory-heap-crash",
    "db-wal-recovery", "distribution-search", "dna-insert", "extract-elf",
    "filter-js-from-html", "financial-document-processor", "gcode-to-text",
    "git-leak-recovery", "git-multibranch", "hf-model-inference", "kv-store-grpc",
    "large-scale-text-editing", "largest-eigenval", "log-summary-date-ranges",
    "mailman", "mteb-leaderboard", "mteb-retrieve", "multi-source-data-merger",
    "nginx-request-logging", "openssl-selfsigned-cert", "portfolio-optimization",
    "pypi-server", "pytorch-model-cli", "pytorch-model-recovery", "qemu-alpine-ssh",
    "qemu-startup", "query-optimize", "raman-fitting", "reshard-c4-data",
    "rstan-to-pystan", "sqlite-db-truncate", "sqlite-with-gcov", "tune-mjcf",
}
_TB2_HARD = {
    "bn-fit-modify", "cancel-async-tasks", "circuit-fibsqrt",
    "configure-git-webserver", "dna-assembly", "extract-moves-from-video",
    "feal-differential-cryptanalysis", "feal-linear-cryptanalysis",
    "fix-code-vulnerability", "fix-ocaml-gc", "gpt2-codegolf", "headless-terminal",
    "install-windows-3.11", "llm-inference-batching-scheduler", "make-doom-for-mips",
    "make-mips-interpreter", "mcmc-sampling-stan", "merge-diff-arc-agi-task",
    "model-extraction-relu-logits", "path-tracing", "path-tracing-reverse",
    "polyglot-c-py", "polyglot-rust-c", "protein-assembly", "regex-chess",
    "sam-cell-seg", "sanitize-git-repo", "schemelike-metacircular-eval",
    "sparql-university", "torch-pipeline-parallelism", "torch-tensor-parallelism",
    "train-fasttext",
}
# Tasks without official difficulty — treat as medium
_TB2_UNKNOWN = {
    "modernize-scientific-stack", "password-recovery", "regex-log",
    "video-processing", "vulnerable-secret", "winning-avg-corewars",
    "write-compressor",
}


def _difficulty(task_name: str) -> str:
    if task_name in _TB2_EASY:
        return "easy"
    if task_name in _TB2_HARD:
        return "hard"
    return "medium"


def _stratified_sample(
    tasks: list[dict[str, Any]], n: int, seed: int = 42
) -> list[dict[str, Any]]:
    """Sample n tasks preserving approximate difficulty distribution."""
    rng = random.Random(seed)
    by_diff: dict[str, list[dict[str, Any]]] = {"easy": [], "medium": [], "hard": []}
    for t in tasks:
        by_diff[_difficulty(t["name"])].append(t)

    total = len(tasks)
    sampled = []
    for diff in ("easy", "medium", "hard"):
        pool = by_diff[diff]
        k = max(1, round(n * len(pool) / total)) if pool else 0
        k = min(k, len(pool))
        sampled.extend(rng.sample(pool, k))

    # If rounding left us short/over, adjust from the largest pool
    while len(sampled) < n:
        remaining = [t for t in tasks if t not in sampled]
        if not remaining:
            break
        sampled.append(rng.choice(remaining))
    while len(sampled) > n:
        sampled.pop()

    rng.shuffle(sampled)
    return sampled


async def _load_tasks_from_harbor(
    dataset_name: str, version: str
) -> list[dict[str, Any]]:
    """Load task metadata from a Harbor dataset.

    Returns a list of dicts with 'name', 'task_dir', and 'instruction'.
    Downloads tasks to the local cache if not already present.
    """
    from harbor.models.job.config import DatasetConfig
    from harbor.models.task.task import Task
    from harbor.tasks.client import TaskClient

    config = DatasetConfig(name=dataset_name, version=version)
    task_configs = await config.get_task_configs()

    # Download any remote tasks to local cache
    remote = [tc for tc in task_configs if not tc.is_local()]
    if remote:
        client = TaskClient()
        task_ids = [tc.get_task_id() for tc in remote]
        await client.download_tasks(task_ids)

    tasks = []
    for tc in task_configs:
        task_dir = tc.get_task_id().get_local_path()
        try:
            task = Task(task_dir)
            tasks.append({
                "name": task.name,
                "task_dir": str(task.task_dir),
                "instruction": task.instruction,
                "difficulty": _difficulty(task.name),
            })
        except Exception as e:
            logger.warning("Failed to load task at %s: %s", task_dir, e)
    return tasks


class TerminalBenchHarness(DomainHarness):
    """Evolve harness for TerminalBench terminal-based coding tasks.

    The task agent is dropped into a Linux container with a task description
    and must solve it using terminal commands. Performance is measured by
    Harbor's verifier (test.sh execution → reward.txt).

    Harness kwargs:
        train_n: Number of training scenarios from tb-pro (default: 25)
        staged_n: Number of staged/validation scenarios from tb-pro (default: 5)
        test_n: Number of test scenarios from TB2 (default: 30)
        n_concurrent: Max concurrent Docker containers (default: 4)
        max_turns: Max agent turns per scenario (default: 30)
        turn_timeout: Timeout per command in seconds (default: 120)
        cost_ceiling_usd: Max cost per scenario in USD (default: 2.0)
        trace_dir: Directory to save execution traces (default: None)
    """

    def __init__(
        self,
        train_n: int | str = 25,
        staged_n: int | str = 5,
        test_n: int | str = 30,
        n_concurrent: int | str = 4,
        max_turns: int | str = 30,
        turn_timeout: int | str = 120,
        cost_ceiling_usd: float | str = 2.0,
        trace_dir: str | None = None,
    ) -> None:
        self._train_n = int(train_n)
        self._staged_n = int(staged_n)
        self._test_n = int(test_n)
        self._n_concurrent = int(n_concurrent)
        self._max_turns = int(max_turns)
        self._turn_timeout = int(turn_timeout)
        self._cost_ceiling_usd = float(cost_ceiling_usd)
        self._trace_dir: Path | None = Path(trace_dir) if trace_dir else None

        # Lazily loaded
        self._train_tasks: list[dict[str, Any]] | None = None
        self._staged_tasks: list[dict[str, Any]] | None = None
        self._test_tasks: list[dict[str, Any]] | None = None
        self._initialized = False

        # Set by the evolve loop before eval — used to build fresh agents per scenario
        self._workspace: Path | None = None
        self._agent_config: Any = None

    async def _ensure_loaded(self) -> None:
        """Lazily load tasks from Harbor on first use."""
        if self._initialized:
            return

        # Load tb-pro for train + staged
        pro_tasks = await _load_tasks_from_harbor("terminal-bench-pro", "0.1.1")
        rng = random.Random(42)
        rng.shuffle(pro_tasks)

        # Split: first staged_n for staged, rest for train pool
        self._staged_tasks = pro_tasks[: self._staged_n]
        train_pool = pro_tasks[self._staged_n :]
        self._train_tasks = train_pool[: self._train_n]

        # Load TB2 for test — stratified subset
        tb2_tasks = await _load_tasks_from_harbor("terminal-bench", "2.0")
        self._test_tasks = _stratified_sample(tb2_tasks, self._test_n)

        self._initialized = True
        logger.info(
            "TerminalBench loaded: train=%d, staged=%d, test=%d",
            len(self._train_tasks),
            len(self._staged_tasks),
            len(self._test_tasks),
        )

    async def ensure_loaded(self) -> None:
        """Public async initialization — call before using scenarios/staged/test."""
        await self._ensure_loaded()

    def scenarios(self) -> list[dict[str, Any]]:
        """Return training scenarios (tb-pro tasks). Must call ensure_loaded() first."""
        if not self._initialized:
            raise RuntimeError("Call await harness.ensure_loaded() before scenarios()")
        return self._train_tasks  # type: ignore[return-value]

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        """Return disjoint validation subset from tb-pro. Must call ensure_loaded() first."""
        if not self._initialized:
            raise RuntimeError("Call await harness.ensure_loaded() before staged_sample()")
        return self._staged_tasks[:n]  # type: ignore[index]

    def test_scenarios(self) -> list[dict[str, Any]]:
        """Return held-out TB2 test scenarios. Must call ensure_loaded() first."""
        if not self._initialized:
            raise RuntimeError("Call await harness.ensure_loaded() before test_scenarios()")
        return self._test_tasks  # type: ignore[return-value]

    async def run_agent(
        self, agent: Agent, scenario: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the agent on a single task inside a Docker container.

        Builds a fresh agent per scenario to avoid shared-state races under
        concurrent execution. The ``agent`` arg is used only as a fallback if
        ``_workspace``/``_agent_config`` aren't set (e.g., called from evolve-eval).
        """
        from harbor.environments.docker.docker import DockerEnvironment
        from harbor.models.task.task import Task
        from harbor.models.trial.paths import TrialPaths
        from harbor.verifier.verifier import Verifier

        task_name = scenario["name"]
        task = Task(scenario["task_dir"])

        # Create trial directory for Harbor's file I/O
        trial_dir = Path(tempfile.mkdtemp(prefix=f"tb_evolve_{task_name}_"))
        trial_paths = TrialPaths(trial_dir=trial_dir)
        trial_paths.mkdir()

        env = DockerEnvironment(
            environment_dir=task.paths.environment_dir,
            environment_name=task_name,
            session_id=f"evolve__{task_name}__{uuid.uuid4().hex[:8]}",
            trial_paths=trial_paths,
            task_env_config=task.config.environment,
        )

        result: dict[str, Any] = {
            "scenario_id": task_name,
            "difficulty": scenario.get("difficulty", "unknown"),
            "success": False,
            "reward": 0.0,
            "error": None,
            "tokens_used": 0,
        }

        try:
            await env.start(force_build=False)

            # Build a fresh agent for this scenario (avoids shared-state races)
            if self._workspace is not None and self._agent_config is not None:
                task_agent = build_agent_from_workspace(self._workspace, self._agent_config)
            else:
                task_agent = agent

            # Inject Docker environment into all workspace tools that accept it
            _inject_environment(task_agent, env)

            # Ensure there's at least a bash tool
            if task_agent.registry.get("bash") is None:
                task_agent.registry.register(
                    DockerBashHandler(env, timeout_sec=self._turn_timeout)
                )

            # Set up trace saving
            trace_path = None
            if self._trace_dir is not None:
                trace_task_dir = self._trace_dir / task_name
                trace_task_dir.mkdir(parents=True, exist_ok=True)
                trace_path = trace_task_dir / "trace.jsonl"

            state = State(trace_path=trace_path) if trace_path else State()

            async with Session(task_agent, state=state) as session:
                run_result = await session.run(
                    scenario["instruction"],
                    max_turns=self._max_turns,
                )

            usage = getattr(run_result, "usage", None)
            if usage:
                result["tokens_used"] = (
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                )

            # Verify with Harbor
            verifier = Verifier(
                task=task,
                trial_paths=trial_paths,
                environment=env,
            )
            verify_result = await verifier.verify()
            reward = verify_result.rewards.get("reward", 0.0)
            result["reward"] = reward
            result["success"] = float(reward) >= 1.0

        except Exception as e:
            result["error"] = str(e)
            logger.warning("Task %s failed: %s", task_name, e)
        finally:
            try:
                await env.stop(delete=True)
            except Exception:
                pass
            # Clean up trial dir
            shutil.rmtree(trial_dir, ignore_errors=True)

        return result

    async def run_all(
        self, agent: Agent, scenarios: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run all scenarios concurrently with a semaphore."""
        sem = asyncio.Semaphore(self._n_concurrent)

        async def _run_one(scenario: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    return await self.run_agent(agent, scenario)
                except Exception as e:
                    return {
                        "scenario_id": scenario.get("name", "unknown"),
                        "success": False,
                        "error": str(e),
                        "tokens_used": 0,
                    }

        return await asyncio.gather(*[_run_one(s) for s in scenarios])

    def score(self, results: list[dict[str, Any]]) -> float:
        """Success rate (fraction of tasks solved)."""
        if not results:
            return 0.0
        solved = sum(1 for r in results if r.get("success", False))
        return solved / len(results)

    def feedback(self, results: list[dict[str, Any]]) -> str:
        """Rich feedback with per-difficulty breakdown, error categorization, and token usage."""
        if not results:
            return "No results to analyze."

        total = len(results)
        solved = sum(1 for r in results if r.get("success"))
        accuracy = solved / total

        # Per-difficulty breakdown
        by_diff: dict[str, list[dict[str, Any]]] = {}
        for r in results:
            d = r.get("difficulty", "unknown")
            by_diff.setdefault(d, []).append(r)

        lines = [
            f"Overall: {accuracy:.1%} ({solved}/{total})",
            "",
            "By difficulty:",
        ]
        for diff in ("easy", "medium", "hard", "unknown"):
            group = by_diff.get(diff, [])
            if not group:
                continue
            g_solved = sum(1 for r in group if r.get("success"))
            lines.append(f"  {diff}: {g_solved}/{len(group)} ({g_solved/len(group):.0%})")

        # Token usage
        total_tokens = sum(r.get("tokens_used", 0) for r in results)
        avg_tokens = total_tokens / total if total else 0
        solved_tokens = [r.get("tokens_used", 0) for r in results if r.get("success")]
        failed_tokens = [r.get("tokens_used", 0) for r in results if not r.get("success")]
        lines.extend([
            "",
            f"Token usage: {total_tokens:,} total, {avg_tokens:,.0f} avg per task",
        ])
        if solved_tokens:
            lines.append(f"  Solved tasks avg: {sum(solved_tokens)/len(solved_tokens):,.0f} tokens")
        if failed_tokens:
            lines.append(f"  Failed tasks avg: {sum(failed_tokens)/len(failed_tokens):,.0f} tokens")

        # Error categorization
        failures = [r for r in results if not r.get("success")]
        if failures:
            error_types: dict[str, int] = {}
            for f in failures:
                err = f.get("error") or "wrong answer (tests failed)"
                # Categorize
                if "timeout" in err.lower():
                    cat = "timeout"
                elif "error executing" in err.lower() or "exception" in err.lower():
                    cat = "execution error"
                elif f.get("reward", 0) > 0:
                    cat = "partial (reward > 0)"
                else:
                    cat = "wrong answer"
                error_types[cat] = error_types.get(cat, 0) + 1

            lines.extend(["", "Failure breakdown:"])
            for cat, count in sorted(error_types.items(), key=lambda x: -x[1]):
                lines.append(f"  {cat}: {count}")

            lines.extend([
                "",
                f"Failed tasks ({min(15, len(failures))} of {len(failures)}):",
            ])
            for f in failures[:15]:
                err_short = (f.get("error") or "tests failed")[:80]
                lines.append(f"  - {f['scenario_id']} [{f.get('difficulty', '?')}]: {err_short}")

        return "\n".join(lines)

    def set_trace_dir(self, trace_dir: Path) -> None:
        """Set the trace directory (called by the evolve loop before eval)."""
        self._trace_dir = trace_dir

    def set_workspace(self, workspace: Path, config: Any) -> None:
        """Set workspace + config so run_agent can build fresh agents per scenario."""
        self._workspace = workspace
        self._agent_config = config


def _inject_environment(agent: Agent, env: Any) -> None:
    """Inject the Docker environment into all workspace tools that accept it.

    Any ToolHandler with an ``environment`` or ``_environment`` attribute
    gets the running DockerEnvironment set on it. This lets the meta-agent
    create arbitrary tools that use ``self.environment`` without special wiring.
    """
    for name in list(agent.registry._handlers):
        handler = agent.registry._handlers[name]
        if hasattr(handler, "environment"):
            handler.environment = env
        if hasattr(handler, "_environment"):
            handler._environment = env
