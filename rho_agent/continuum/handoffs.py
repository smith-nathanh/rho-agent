"""Read/write handoff documents in .rho-agent/handoffs/."""

from __future__ import annotations

import re
from pathlib import Path


def handoffs_dir(working_dir: str) -> Path:
    """Return the handoffs directory path."""
    return Path(working_dir) / ".rho-agent" / "handoffs"


def ensure_handoffs_dir(working_dir: str) -> Path:
    """Create the handoffs directory and add .rho-agent/ to .gitignore."""
    hdir = handoffs_dir(working_dir)
    hdir.mkdir(parents=True, exist_ok=True)

    gitignore = Path(working_dir) / ".gitignore"
    marker = ".rho-agent/"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if marker not in content:
            if not content.endswith("\n"):
                content += "\n"
            content += f"{marker}\n"
            gitignore.write_text(content, encoding="utf-8")
    else:
        gitignore.write_text(f"{marker}\n", encoding="utf-8")

    return hdir


def _handoff_files(working_dir: str) -> list[Path]:
    """Return sorted list of handoff files by number."""
    hdir = handoffs_dir(working_dir)
    if not hdir.exists():
        return []
    files = [f for f in hdir.glob("*.md") if re.match(r"\d+-", f.name)]
    return sorted(files, key=lambda f: f.name)


def latest_handoff(working_dir: str) -> str | None:
    """Read content of the highest-numbered handoff, or None."""
    files = _handoff_files(working_dir)
    if not files:
        return None
    return files[-1].read_text(encoding="utf-8")


def latest_handoff_number(working_dir: str) -> int:
    """Return the number of the latest handoff, or 0."""
    files = _handoff_files(working_dir)
    if not files:
        return 0
    match = re.match(r"(\d+)", files[-1].name)
    return int(match.group(1)) if match else 0


def write_handoff(working_dir: str, number: int, slug: str, content: str) -> Path:
    """Write a handoff document and return its path."""
    hdir = ensure_handoffs_dir(working_dir)
    filename = f"{number:03d}-{slug}.md"
    path = hdir / filename
    path.write_text(content, encoding="utf-8")
    return path
