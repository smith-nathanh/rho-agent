"""Domain harness ABC and loader."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any

from ..core.agent import Agent


class DomainHarness(ABC):
    """Abstract base for domain-specific evaluation harnesses.

    Subclass this to define how task-agents are evaluated in a specific domain
    (prediction markets, security auditing, research, etc.).
    """

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

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        """Return a small subset of scenarios for quick filtering."""
        return self.scenarios()[:n]


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
