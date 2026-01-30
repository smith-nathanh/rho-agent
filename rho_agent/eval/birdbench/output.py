"""Output formatting for BIRD-Bench evaluation results."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BirdMetrics, TaskResult


def create_run_dir(base_dir: Path | str) -> Path:
    """Create a timestamped run directory."""
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base_dir / f"run-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Update 'latest' symlink
    latest_link = base_dir / "latest"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    os.symlink(run_dir.name, latest_link)

    return run_dir


def append_result(result: TaskResult, output_dir: Path | str) -> None:
    """Append a single result to runs.jsonl."""
    runs_path = Path(output_dir) / "runs.jsonl"
    with open(runs_path, "a", encoding="utf-8") as f:
        line = json.dumps(result.to_dict(), ensure_ascii=False)
        f.write(line + "\n")


def update_overall(metrics: BirdMetrics, output_dir: Path | str) -> None:
    """Update overall.json and summary.txt with current metrics."""
    output_dir = Path(output_dir)

    with open(output_dir / "overall.json", "w", encoding="utf-8") as f:
        json.dump(metrics.to_dict(), f, indent=2, ensure_ascii=False)

    with open(output_dir / "summary.txt", "w", encoding="utf-8") as f:
        f.write(format_summary(metrics))
        f.write("\n")


def get_completed_indices(output_dir: Path | str) -> set[int]:
    """Load completed task indices from existing runs.jsonl."""
    runs_path = Path(output_dir) / "runs.jsonl"
    if not runs_path.exists():
        return set()

    indices = set()
    with open(runs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                indices.add(data["index"])
    return indices


def save_run_config(config: dict[str, Any], output_dir: Path | str) -> None:
    """Save run configuration for reproducibility."""
    config_path = Path(output_dir) / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def format_summary(metrics: BirdMetrics) -> str:
    """Format a summary of BIRD-Bench evaluation metrics."""
    lines = [
        "=" * 55,
        "BIRD-Bench Evaluation Results",
        "=" * 55,
        f"Total tasks:     {metrics.total}",
        f"Passed (EX):     {metrics.passed}",
        f"Failed:          {metrics.failed}",
        f"Accuracy:        {metrics.accuracy:.1%}",
        "",
        "By Difficulty:",
    ]

    for diff in ["simple", "moderate", "challenging"]:
        t = metrics.difficulty_total.get(diff, 0)
        p = metrics.difficulty_passed.get(diff, 0)
        acc = p / t if t > 0 else 0.0
        lines.append(f"  {diff:13s}  {p:>4d}/{t:<4d} ({acc:.1%})")

    lines.append("")
    lines.append("By Database:")

    for db in sorted(metrics.db_total):
        t = metrics.db_total[db]
        p = metrics.db_passed.get(db, 0)
        acc = p / t if t > 0 else 0.0
        lines.append(f"  {db:30s}  {p:>3d}/{t:<3d} ({acc:.1%})")

    lines.extend([
        "",
        "Status Breakdown:",
        f"  Completed:           {metrics.completed}",
        f"  Context limit:       {metrics.context_limit}",
        f"  Turn limit reached:  {metrics.task_limit_reached}",
        f"  Task error:          {metrics.task_error}",
        "=" * 55,
    ])

    return "\n".join(lines)


def rebuild_metrics_from_runs(output_dir: Path | str) -> BirdMetrics:
    """Rebuild metrics from all results in runs.jsonl (for resume)."""
    runs_path = Path(output_dir) / "runs.jsonl"
    metrics = BirdMetrics()

    with open(runs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)

            metrics.total += 1
            result_data = r.get("result", {})
            is_correct = result_data.get("is_correct", False)
            difficulty = result_data.get("difficulty", "unknown")
            db_id = result_data.get("db_id", "unknown")

            if is_correct:
                metrics.passed += 1
            else:
                metrics.failed += 1

            # Status
            status = r.get("status", "unknown")
            if status == "completed":
                metrics.completed += 1
            elif status == "agent context limit":
                metrics.context_limit += 1
            elif status == "task limit reached":
                metrics.task_limit_reached += 1
            elif status == "task error":
                metrics.task_error += 1

            # Difficulty
            metrics.difficulty_total[difficulty] = metrics.difficulty_total.get(difficulty, 0) + 1
            if is_correct:
                metrics.difficulty_passed[difficulty] = metrics.difficulty_passed.get(difficulty, 0) + 1

            # Database
            metrics.db_total[db_id] = metrics.db_total.get(db_id, 0) + 1
            if is_correct:
                metrics.db_passed[db_id] = metrics.db_passed.get(db_id, 0) + 1

            metrics.history_lengths.append(len(r.get("history", [])))

    return metrics
