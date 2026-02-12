"""Atomic state persistence for conductor runs."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import ConductorState, TaskStatus


def _default_state_dir() -> Path:
    return Path.home() / ".config" / "rho-agent" / "conductor"


def state_path_for_run(run_id: str) -> Path:
    """Return the default state file path for a run."""
    return _default_state_dir() / f"{run_id}.json"


def latest_state_path() -> Path | None:
    """Return the most recently modified default state file, if any."""
    state_dir = _default_state_dir()
    if not state_dir.exists():
        return None
    paths = [p for p in state_dir.glob("*.json") if p.is_file()]
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def save_state(path: Path, state: ConductorState) -> None:
    """Atomically write state to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state.to_dict(), indent=2)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_state(path: Path) -> ConductorState:
    """Load state from JSON, resetting stale IN_PROGRESS tasks to PENDING."""
    data = json.loads(path.read_text(encoding="utf-8"))
    state = ConductorState.from_dict(data)
    if state.dag:
        for task in state.dag.tasks.values():
            if task.status == TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.PENDING
    return state
