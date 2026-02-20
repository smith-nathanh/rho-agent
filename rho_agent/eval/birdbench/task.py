"""BIRD-Bench task loader and data structures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class BirdTask:
    """A BIRD-Bench text-to-SQL evaluation task."""

    index: int
    question_id: int
    db_id: str
    question: str
    evidence: str  # domain knowledge hint (may be empty)
    gold_sql: str  # reference SQL
    difficulty: str  # "simple", "moderate", "challenging"
    db_path: str  # absolute path to .sqlite file
    include_evidence: bool = True

    def get_prompt(self) -> str:
        """Build the prompt the agent sees.

        Includes the question and evidence hint (if enabled and non-empty).
        Does NOT include the gold SQL.
        """
        parts = [self.question]

        if self.include_evidence and self.evidence:
            parts.append(f"\nHint: {self.evidence}")

        parts.append(
            f"\nDatabase: {self.db_id}"
            "\n\nUse execute_sql to explore the schema and data, then write a SQL query "
            "to answer the question. When you have your final SQL, use submit_sql to submit it."
        )

        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "index": self.index,
            "question_id": self.question_id,
            "db_id": self.db_id,
            "question": self.question,
            "evidence": self.evidence,
            "gold_sql": self.gold_sql,
            "difficulty": self.difficulty,
        }


def load_bird_tasks(
    data_file: Path | str,
    db_dir: Path | str,
    include_evidence: bool = True,
    difficulty: str | None = None,
) -> list[BirdTask]:
    """Load BIRD tasks from JSON file.

    Args:
        data_file: Path to task JSON file (e.g., mini_dev_sqlite.json).
        db_dir: Path to dev_databases/ directory containing SQLite files.
        include_evidence: Whether to include evidence hints in prompts.
        difficulty: Optional filter by difficulty level.

    Returns:
        List of BirdTask objects.
    """
    data_file = Path(data_file)
    db_dir = Path(db_dir)

    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    tasks = []
    for idx, item in enumerate(data):
        db_id = item["db_id"]
        task_difficulty = item.get("difficulty", "unknown")

        # Filter by difficulty if specified
        if difficulty and task_difficulty != difficulty:
            continue

        # Resolve database path
        db_path = db_dir / db_id / f"{db_id}.sqlite"
        if not db_path.exists():
            raise FileNotFoundError(
                f"Database not found: {db_path}\n"
                f"Make sure dev_databases/ is downloaded and extracted."
            )

        task = BirdTask(
            index=idx,
            question_id=item["question_id"],
            db_id=db_id,
            question=item["question"],
            evidence=item.get("evidence", ""),
            gold_sql=item["SQL"],
            difficulty=task_difficulty,
            db_path=str(db_path),
            include_evidence=include_evidence,
        )
        tasks.append(task)

    return tasks
