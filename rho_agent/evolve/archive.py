"""JSONL archive for tracking generations."""

from __future__ import annotations

import json
import random
from pathlib import Path

from .models import Generation


def append_generation(path: str | Path, gen: Generation) -> None:
    """Append a generation record to the JSONL archive."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(gen.to_dict()) + "\n")


def load_archive(path: str | Path) -> list[Generation]:
    """Read all generations from the JSONL archive."""
    path = Path(path)
    if not path.exists():
        return []
    generations = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                generations.append(Generation.from_dict(json.loads(line)))
    return generations


def best_generation(path: str | Path) -> Generation | None:
    """Return the highest-scoring generation, or None if archive is empty."""
    scored = [g for g in load_archive(path) if g.score is not None]
    if not scored:
        return None
    return max(scored, key=lambda g: g.score)  # type: ignore[arg-type]


def _child_counts(archive: list[Generation]) -> dict[str, int]:
    """Count how many children each generation has."""
    counts: dict[str, int] = {g.gen_id: 0 for g in archive}
    for g in archive:
        if g.parent_id and g.parent_id in counts:
            counts[g.parent_id] += 1
    return counts


def mark_invalid_parent(path: str | Path, gen_id: str) -> None:
    """Mark a generation as an invalid parent by rewriting the archive.

    Called when the meta-agent fails on a parent, indicating this node
    consistently produces broken children.
    """
    path = Path(path)
    archive = load_archive(path)
    updated = False
    for g in archive:
        if g.gen_id == gen_id and g.valid_parent:
            g.valid_parent = False
            updated = True
    if updated:
        with open(path, "w", encoding="utf-8") as f:
            for g in archive:
                f.write(json.dumps(g.to_dict()) + "\n")


def select_parent(path: str | Path, strategy: str = "best") -> Generation | None:
    """Select a parent generation for the next iteration.

    Only considers generations marked as valid_parent=True.

    Strategies:
        best: Always pick the highest-scoring generation.
        recent_best: Pick the best from the last 5 generations.
        tournament: Random selection weighted toward higher scores.
        score_child_prop: Score-proportional, inversely weighted by child count.
            Matches HyperAgents' default — biases toward high scorers while
            preserving exploration by favoring nodes with fewer children.
    """
    archive = load_archive(path)
    scored = [g for g in archive if g.score is not None and g.valid_parent]
    if not scored:
        return None

    if strategy == "best":
        return max(scored, key=lambda g: g.score)  # type: ignore[arg-type]

    elif strategy == "recent_best":
        recent = scored[-5:]
        return max(recent, key=lambda g: g.score)  # type: ignore[arg-type]

    elif strategy == "tournament":
        candidates = random.sample(scored, min(3, len(scored)))
        return max(candidates, key=lambda g: g.score)  # type: ignore[arg-type]

    elif strategy == "score_child_prop":
        children = _child_counts(archive)
        # Weight = score / (1 + num_children)
        weights = []
        for g in scored:
            s = max(g.score, 0.001)  # type: ignore[arg-type]  # avoid zero weights
            w = s / (1 + children.get(g.gen_id, 0))
            weights.append(w)
        total = sum(weights)
        if total == 0:
            return random.choice(scored)
        r = random.random() * total
        cumulative = 0.0
        for g, w in zip(scored, weights):
            cumulative += w
            if r <= cumulative:
                return g
        return scored[-1]  # fallback

    else:
        raise ValueError(f"Unknown parent selection strategy: {strategy}")
