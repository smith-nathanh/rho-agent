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


def select_parent(path: str | Path, strategy: str = "best") -> Generation | None:
    """Select a parent generation for the next iteration.

    Strategies:
        best: Always pick the highest-scoring generation.
        recent_best: Pick the best from the last 5 generations.
        tournament: Random selection weighted toward higher scores.
    """
    scored = [g for g in load_archive(path) if g.score is not None]
    if not scored:
        return None

    if strategy == "best":
        return max(scored, key=lambda g: g.score)  # type: ignore[arg-type]
    elif strategy == "recent_best":
        recent = scored[-5:]
        return max(recent, key=lambda g: g.score)  # type: ignore[arg-type]
    elif strategy == "tournament":
        # Tournament selection: pick 3 random candidates, return best
        candidates = random.sample(scored, min(3, len(scored)))
        return max(candidates, key=lambda g: g.score)  # type: ignore[arg-type]
    else:
        raise ValueError(f"Unknown parent selection strategy: {strategy}")
