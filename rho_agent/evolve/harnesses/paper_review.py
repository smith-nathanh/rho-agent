"""Paper review domain harness.

Evaluates an agent's ability to predict accept/reject decisions for research
papers, scored against observed human reviewer outcomes. Dataset sourced from
the HyperAgents paper (Zhao et al., 2026).
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

from ...core.agent import Agent
from ...core.session import Session
from ..harness import DomainHarness

# The paper texts can be very large
csv.field_size_limit(sys.maxsize)

_DEFAULT_DATASET = Path.home() / "proj" / "HyperAgents" / "domains" / "paper_review" / "dataset.csv"

# Truncate paper text to keep context manageable for the task agent.
# 40k chars ≈ ~10k tokens — enough for abstract + intro + methods + conclusion.
_DEFAULT_MAX_CHARS = 40_000


def _load_dataset(path: Path, max_chars: int) -> list[dict[str, Any]]:
    """Load the paper review CSV into a list of scenario dicts."""
    scenarios = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = row["paper_text"]
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[... truncated ...]"
            scenarios.append({
                "id": row["question_id"],
                "paper_text": text,
                "outcome": row["outcome"],  # "accept" or "reject"
            })
    return scenarios


def _stable_shuffle(scenarios: list[dict[str, Any]], seed: int = 42) -> list[dict[str, Any]]:
    """Deterministic shuffle for reproducible train/val/test splits."""
    shuffled = list(scenarios)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def _balanced_take(
    accepts: list[dict[str, Any]],
    rejects: list[dict[str, Any]],
    n: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Take n items balanced across accept/reject, starting at offset in each list."""
    half = n // 2
    taken = accepts[offset : offset + half] + rejects[offset : offset + (n - half)]
    return taken


def _split_dataset(
    scenarios: list[dict[str, Any]],
    train_n: int = 100,
    val_n: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """Split into balanced train/val/test sets (equal accept/reject per split)."""
    shuffled = _stable_shuffle(scenarios)
    accepts = [s for s in shuffled if s["outcome"] == "accept"]
    rejects = [s for s in shuffled if s["outcome"] == "reject"]

    train_half = train_n // 2
    val_half = val_n // 2

    train = _balanced_take(accepts, rejects, train_n, offset=0)
    val = _balanced_take(accepts, rejects, val_n, offset=train_half)
    # Test gets everything remaining
    used_accept = train_half + val_half
    used_reject = (train_n - train_half) + (val_n - val_half)
    test = accepts[used_accept:] + rejects[used_reject:]

    return {"train": train, "val": val, "test": test}


def _extract_decision(text: str) -> str:
    """Extract accept/reject from agent output. Defaults to reject if unclear."""
    lower = text.lower()
    # Look for explicit decision markers
    for marker in ["decision: accept", "**accept**", "verdict: accept", "i recommend accept"]:
        if marker in lower:
            return "accept"
    for marker in ["decision: reject", "**reject**", "verdict: reject", "i recommend reject"]:
        if marker in lower:
            return "reject"
    # Fall back to last occurrence
    last_accept = lower.rfind("accept")
    last_reject = lower.rfind("reject")
    if last_accept > last_reject:
        return "accept"
    if last_reject > last_accept:
        return "reject"
    return "reject"  # default


class PaperReviewHarness(DomainHarness):
    """Predict accept/reject for ML conference papers.

    The task agent receives the full text of a research paper and must output
    a binary accept/reject decision. Accuracy is measured against observed
    human reviewer outcomes from top-tier ML conferences.

    Harness kwargs:
        dataset_path: Path to the CSV (default: HyperAgents dataset)
        max_chars: Max paper text characters (default: 40000)
        train_n: Number of training scenarios (default: 100)
        val_n: Number of validation scenarios (default: 100)
        max_turns: Max agent turns per scenario (default: 5)
    """

    def __init__(
        self,
        dataset_path: str | None = None,
        max_chars: int | str = _DEFAULT_MAX_CHARS,
        train_n: int | str = 100,
        val_n: int | str = 100,
        max_turns: int | str = 5,
    ) -> None:
        # CLI passes strings via --harness-arg; coerce to int
        max_chars = int(max_chars)
        train_n = int(train_n)
        val_n = int(val_n)
        max_turns = int(max_turns)
        path = Path(dataset_path) if dataset_path else _DEFAULT_DATASET
        if not path.exists():
            raise FileNotFoundError(
                f"Paper review dataset not found: {path}\n"
                "Expected HyperAgents dataset at ~/proj/HyperAgents/domains/paper_review/dataset.csv"
            )
        all_scenarios = _load_dataset(path, max_chars)
        self._splits = _split_dataset(all_scenarios, train_n, val_n)
        self._max_turns = max_turns

    def scenarios(self) -> list[dict[str, Any]]:
        """Return training scenarios (used for full eval)."""
        return self._splits["train"]

    def staged_sample(self, n: int) -> list[dict[str, Any]]:
        """Return a small validation subset for quick filtering."""
        return self._splits["val"][:n]

    async def run_agent(self, agent: Agent, scenario: dict[str, Any]) -> dict[str, Any]:
        """Run the agent on a single paper and extract its decision."""
        prompt = (
            "Review the following research paper and predict whether it will be "
            "accepted or rejected at a top-tier ML conference. "
            "You MUST end your response with exactly one of: "
            "'Decision: accept' or 'Decision: reject'\n\n"
            f"--- PAPER ---\n{scenario['paper_text']}"
        )

        async with Session(agent) as session:
            result = await session.run(prompt, max_turns=self._max_turns)

        prediction = _extract_decision(result.text)
        correct = prediction == scenario["outcome"]

        return {
            "scenario_id": scenario["id"],
            "success": correct,
            "prediction": prediction,
            "expected": scenario["outcome"],
            "agent_text": result.text[:500],  # truncate for archive readability
            "error": None if result.status == "completed" else result.status,
        }

    def score(self, results: list[dict[str, Any]]) -> float:
        """Accuracy: fraction of correct predictions."""
        if not results:
            return 0.0
        correct = sum(1 for r in results if r.get("success", False))
        return correct / len(results)

    def feedback(self, results: list[dict[str, Any]]) -> str:
        """Detailed feedback showing accuracy breakdown and failure patterns."""
        if not results:
            return "No results to analyze."

        total = len(results)
        correct = sum(1 for r in results if r.get("success"))
        accuracy = correct / total

        # Break down by expected outcome
        accept_results = [r for r in results if r.get("expected") == "accept"]
        reject_results = [r for r in results if r.get("expected") == "reject"]

        accept_correct = sum(1 for r in accept_results if r.get("success"))
        reject_correct = sum(1 for r in reject_results if r.get("success"))

        lines = [
            f"Overall accuracy: {accuracy:.1%} ({correct}/{total})",
            f"Accept recall: {accept_correct}/{len(accept_results)} "
            f"({accept_correct/len(accept_results):.1%})" if accept_results else "No accept papers",
            f"Reject recall: {reject_correct}/{len(reject_results)} "
            f"({reject_correct/len(reject_results):.1%})" if reject_results else "No reject papers",
        ]

        # Identify bias
        pred_accepts = sum(1 for r in results if r.get("prediction") == "accept")
        pred_rejects = total - pred_accepts
        lines.append(f"Prediction distribution: {pred_accepts} accept / {pred_rejects} reject")

        if pred_accepts > total * 0.7:
            lines.append("WARNING: Agent is biased toward accepting papers.")
        elif pred_rejects > total * 0.7:
            lines.append("WARNING: Agent is biased toward rejecting papers.")

        # Show some failures
        failures = [r for r in results if not r.get("success")]
        if failures:
            lines.append(f"\nSample failures ({min(5, len(failures))} of {len(failures)}):")
            for f in failures[:5]:
                lines.append(
                    f"  - {f['scenario_id']}: predicted={f.get('prediction')}, "
                    f"expected={f.get('expected')}"
                )

        return "\n".join(lines)
