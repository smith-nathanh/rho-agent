"""Project instructions (AGENTS.md / CLAUDE.md) and skills (SKILL.md).

Both follow the conventions other coding agents use, so a project set up for
one of them works here without changes:

- Instructions: ``AGENTS.md`` in the working directory and each parent up to
  the repository root, plus a personal file. ``CLAUDE.md`` is read where a
  directory has no ``AGENTS.md``.
- Skills: ``<name>/SKILL.md`` folders with ``name`` and ``description`` in YAML
  front matter. Only the name, description and path go into the prompt; the
  agent reads a skill's file when the task calls for it.

Everything here reads the local filesystem, so it applies to the local backend
only. Nothing is ever written.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Instruction file names, in order of preference within one directory.
INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md")
# Combined size limit for instruction files. Later (more specific) files win
# when the limit is reached.
MAX_INSTRUCTION_BYTES = 32 * 1024
# Project skill folders, relative to the working directory or a parent.
PROJECT_SKILL_DIRS = (".agents/skills", ".claude/skills", ".rho-agent/skills")
MAX_SKILLS = 100
MAX_SKILL_DESCRIPTION_CHARS = 500


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    text: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md file


def _config_dir() -> Path:
    return Path.home() / ".config" / "rho-agent"


def _project_dirs(working_dir: Path) -> list[Path]:
    """Directories from the repository root down to the working directory.

    The root is the nearest parent holding ``.git``. Without one, only the
    working directory is used, so files in unrelated parents are not picked up.
    """
    working_dir = working_dir.resolve()
    chain = [working_dir, *working_dir.parents]
    for i, directory in enumerate(chain):
        if (directory / ".git").exists():
            return list(reversed(chain[: i + 1]))
    return [working_dir]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def discover_instructions(working_dir: str | Path | None) -> list[InstructionFile]:
    """Instruction files, most general first: personal, repository root, ... , working dir."""
    candidates: list[Path] = [_config_dir() / "AGENTS.md"]
    for directory in _project_dirs(Path(working_dir or Path.cwd())):
        for name in INSTRUCTION_FILENAMES:
            path = directory / name
            if path.is_file():
                candidates.append(path)
                break

    found: list[InstructionFile] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:  # e.g. CLAUDE.md symlinked to AGENTS.md
            continue
        seen.add(resolved)
        text = _read_text(path)
        if text:
            found.append(InstructionFile(path=path, text=text))

    # Enforce the size limit from the most specific file backwards.
    kept: list[InstructionFile] = []
    budget = MAX_INSTRUCTION_BYTES
    for item in reversed(found):
        size = len(item.text.encode("utf-8"))
        if size > budget:
            if budget > 0:
                clipped = item.text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
                kept.append(InstructionFile(item.path, clipped + "\n[... truncated ...]"))
            break
        budget -= size
        kept.append(item)
    return list(reversed(kept))


def _parse_skill(skill_file: Path) -> Skill | None:
    text = _read_text(skill_file)
    if not text or not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or skill_file.parent.name).strip()
    description = " ".join(str(meta.get("description") or "").split())
    if not name or not description:
        return None
    if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
        description = description[: MAX_SKILL_DESCRIPTION_CHARS - 3] + "..."
    return Skill(name=name, description=description, path=skill_file)


def skill_dirs(working_dir: str | Path | None) -> list[Path]:
    """Skill folders, highest priority first: working dir, parents, then personal."""
    dirs: list[Path] = []
    for directory in reversed(_project_dirs(Path(working_dir or Path.cwd()))):
        dirs.extend(directory / rel for rel in PROJECT_SKILL_DIRS)
    home = Path.home()
    dirs.extend(
        [_config_dir() / "skills", home / ".agents" / "skills", home / ".claude" / "skills"]
    )
    return dirs


def discover_skills(working_dir: str | Path | None) -> list[Skill]:
    """Skills visible from ``working_dir``. A project skill hides a personal one of the same name."""
    skills: dict[str, Skill] = {}
    seen_files: set[Path] = set()
    for directory in skill_dirs(working_dir):
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
            resolved = skill_file.resolve()
            if resolved in seen_files:  # .claude/skills linked to .agents/skills
                continue
            seen_files.add(resolved)
            skill = _parse_skill(skill_file)
            if skill is not None and skill.name not in skills:
                skills[skill.name] = skill
            if len(skills) >= MAX_SKILLS:
                return list(skills.values())
    return list(skills.values())


def render_instructions(files: list[InstructionFile]) -> str:
    if not files:
        return ""
    blocks = [
        "# Project instructions",
        "",
        "These files hold instructions from the user for working in this project. "
        "Follow them. Where two conflict, the later one is more specific and wins.",
    ]
    for item in files:
        blocks += ["", f"## {item.path}", "", item.text]
    return "\n".join(blocks)


def render_skills(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [
        "# Skills",
        "",
        "A skill is a folder of instructions, and sometimes scripts, for one kind of task. "
        "When a task matches a skill below, read its SKILL.md file before starting and "
        "follow it. Paths inside a skill are relative to the folder holding its SKILL.md.",
        "",
    ]
    lines += [f"- {s.name}: {s.description} ({s.path})" for s in skills]
    return "\n".join(lines)


def build_context(
    working_dir: str | Path | None, *, instructions: bool = True, skills: bool = True
) -> str:
    """Text to append to the system prompt, or an empty string."""
    sections = []
    if instructions:
        sections.append(render_instructions(discover_instructions(working_dir)))
    if skills:
        sections.append(render_skills(discover_skills(working_dir)))
    return "\n\n".join(s for s in sections if s)
