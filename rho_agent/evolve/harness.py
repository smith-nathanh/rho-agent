"""Domain harness ABC and loader."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..core.agent import Agent


class DomainHarness(ABC):
    """Abstract base for domain-specific evaluation harnesses.

    Subclass this to define how task-agents are evaluated in a specific domain
    (prediction markets, security auditing, research, etc.).
    """

    async def ensure_loaded(self) -> None:
        """Async initialization hook. Called once before scenarios/eval.

        Override for harnesses that need async setup (e.g., downloading tasks).
        Default: no-op.
        """
        pass

    @abstractmethod
    def scenarios(self) -> list[dict[str, Any]]:
        """Return all evaluation scenarios."""
        ...

    @abstractmethod
    async def run_agent(self, agent: Agent, scenario: dict[str, Any]) -> dict[str, Any]:
        """Run the agent on a single scenario and return results.

        The harness owns the Session lifecycle: it creates its own Session,
        runs it, and controls timeouts/turns.
        """
        ...

    @abstractmethod
    def score(self, results: list[dict[str, Any]]) -> float:
        """Compute an aggregate score from all scenario results."""
        ...

    def feedback(self, results: list[dict[str, Any]]) -> str:
        """Natural-language analysis of results for the meta-agent.

        Default: list failures. Override for domain-specific insight.
        """
        failures = [r for r in results if not r.get("success", False)]
        if not failures:
            return "All scenarios passed."
        lines = [f"Failed {len(failures)}/{len(results)} scenarios:"]
        for f in failures[:10]:
            scenario_id = f.get("scenario_id", "unknown")
            error = f.get("error", "no details")
            lines.append(f"  - {scenario_id}: {error}")
        if len(failures) > 10:
            lines.append(f"  ... and {len(failures) - 10} more")
        return "\n".join(lines)

    async def run_all(
        self, agent: Agent, scenarios: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run the agent on all scenarios and return results.

        Default: sequential. Override for concurrent execution.
        """
        results = []
        for scenario in scenarios:
            try:
                result = await self.run_agent(agent, scenario)
                results.append(result)
            except Exception as e:
                results.append({
                    "scenario_id": scenario.get("id", scenario.get("name", "unknown")),
                    "success": False,
                    "error": str(e),
                })
        return results

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        """Return a small disjoint subset of scenarios for quick filtering.

        Subclasses must override this to provide a validation set that does not
        overlap with the full evaluation scenarios.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement staged_sample() "
            "with a disjoint validation set"
        )

    def set_trace_dir(self, trace_dir: Path | None) -> None:
        """Set trace directory for saving execution traces. Optional override."""
        pass

    def set_workspace(self, workspace: Path, config: Any) -> None:
        """Set workspace + config for building fresh agents per scenario. Optional override."""
        pass


def load_harness(dotted_path: str, **kwargs: Any) -> DomainHarness:
    """Import and instantiate a DomainHarness from a dotted path.

    Example: load_harness("mypackage.harnesses.PredictionHarness", data_dir="/tmp")
    """
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise ValueError(f"Invalid harness path (need module.ClassName): {dotted_path}")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, DomainHarness)):
        raise TypeError(f"{dotted_path} is not a DomainHarness subclass")
    return cls(**kwargs)
