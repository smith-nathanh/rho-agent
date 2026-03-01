"""Atomic state persistence for continuum runs."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import ContinuumState


def _default_state_dir() -> Path:
    return Path.home() / ".config" / "rho-agent" / "continuum"


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


def save_state(path: Path, state: ContinuumState) -> None:
    """Atomically write state to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state.to_dict(), indent=2)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_state(path: Path) -> ContinuumState:
    """Load state from JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ContinuumState.from_dict(data)
