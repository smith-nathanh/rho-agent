"""Workspace directory operations, git-based diff tracking, and dynamic tool loading."""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..core.config import AgentConfig
from ..tools.base import ToolHandler

logger = logging.getLogger(__name__)


# --- Git-based workspace operations ---


def _run_git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the workspace directory."""
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=check,
    )


def create_workspace(run_dir: str, gen_id: str) -> Path:
    """Create an empty workspace as a git repo."""
    workspace = Path(run_dir) / "workspaces" / gen_id
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(exist_ok=True)
    (workspace / "lib").mkdir(exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)

    _run_git(workspace, "init", "-b", "main")
    _run_git(workspace, "config", "user.email", "evolve@rho-agent")
    _run_git(workspace, "config", "user.name", "evolve")
    # Initial empty commit so we always have a HEAD
    _run_git(workspace, "commit", "--allow-empty", "-m", "init")
    return workspace


def materialize_workspace(
    run_dir: str,
    gen_id: str,
    archive: list[Any],
    parent_id: str | None = None,
) -> Path:
    """Materialize a workspace by applying the lineage diff chain from root.

    Creates a fresh git repo, then applies each ancestor's diff in order
    (root → ... → parent) to reconstruct the parent's full workspace state.

    If parent_id is given, the lineage is walked from root to parent_id.
    Otherwise, if gen_id exists in the archive, its own lineage is used
    (for re-materializing an existing generation).
    """
    workspace = create_workspace(run_dir, gen_id)
    diffs_dir = Path(run_dir) / "diffs"

    target = parent_id or gen_id
    chain = get_lineage(target, archive)
    for ancestor in chain:
        diff_path = diffs_dir / f"{ancestor.gen_id}.diff"
        if diff_path.exists() and diff_path.stat().st_size > 0:
            result = _run_git(workspace, "apply", "--allow-empty", str(diff_path), check=False)
            if result.returncode != 0:
                logger.warning(
                    "Failed to apply diff %s: %s", diff_path.name, result.stderr.strip()
                )
            else:
                _run_git(workspace, "add", "-A")
                _run_git(workspace, "commit", "-m", f"apply {ancestor.gen_id}", "--allow-empty")

    return workspace


def commit_pre_mutation(workspace: Path) -> None:
    """Commit current workspace state before the meta-agent mutates it.

    This establishes the baseline so we can extract the diff after mutation.
    """
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "--allow-empty", "-m", "pre-mutation")


def extract_diff(workspace: Path, run_dir: str, gen_id: str) -> Path:
    """Extract the meta-agent's changes as a diff file.

    Diffs the working tree against the last commit (pre-mutation state).
    Returns the path to the saved diff file.
    """
    diffs_dir = Path(run_dir) / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    diff_path = diffs_dir / f"{gen_id}.diff"

    _run_git(workspace, "add", "-A")
    result = _run_git(workspace, "diff", "--cached", "HEAD")
    diff_path.write_text(result.stdout, encoding="utf-8")

    # Commit the mutation so the workspace is clean
    if result.stdout.strip():
        _run_git(workspace, "commit", "-m", f"mutation {gen_id}")

    return diff_path


def get_lineage(gen_id: str, archive: list[Any]) -> list[Any]:
    """Walk parent chain from root to gen_id, returning ancestors in order.

    Returns [root, ..., grandparent, parent, self] — the full chain of
    generations whose diffs must be applied to reconstruct this generation.
    """
    by_id = {g.gen_id: g for g in archive}
    chain = []
    current = by_id.get(gen_id)
    while current is not None:
        chain.append(current)
        current = by_id.get(current.parent_id) if current.parent_id else None
    chain.reverse()
    return chain


# --- Dynamic tool loading ---


def load_tools_from_workspace(workspace: Path) -> list[ToolHandler]:
    """Dynamically import ToolHandler subclasses from workspace/tools/*.py.

    Uses unique module names per workspace to avoid sys.modules collisions.
    Temporarily adds workspace/lib/ to sys.path so tools can import helpers.
    """
    tools_dir = workspace / "tools"
    if not tools_dir.exists():
        return []

    lib_dir = workspace / "lib"
    lib_added = False
    if lib_dir.exists() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
        lib_added = True

    handlers: list[ToolHandler] = []
    try:
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            handler = _load_handler_from_file(py_file, workspace.name)
            if handler is not None:
                handlers.append(handler)
    finally:
        if lib_added:
            sys.path.remove(str(lib_dir))

    return handlers


def _load_handler_from_file(py_file: Path, namespace: str) -> ToolHandler | None:
    """Load a single ToolHandler subclass from a Python file."""
    module_name = f"_evolve_tools_{namespace}_{py_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[module_name]
        raise

    # Find the first ToolHandler subclass defined in this module
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, ToolHandler)
            and attr is not ToolHandler
            and attr.__module__ == module_name
        ):
            return attr()
    return None


# --- Prompt + agent building ---


def load_prompt_from_workspace(workspace: Path) -> str:
    """Read prompt.md from a workspace, returning empty string if missing."""
    prompt_path = workspace / "prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return ""


def build_agent_from_workspace(
    workspace: Path,
    config: Any,
) -> Agent:
    """Assemble a full Agent from workspace contents.

    Loads prompt.md as system prompt, dynamically imports tools from tools/,
    and uses unrestricted profile with the task model.
    """
    prompt = load_prompt_from_workspace(workspace)
    custom_tools = load_tools_from_workspace(workspace)

    agent_config = AgentConfig(
        system_prompt=prompt,
        model=config.effective_task_model,
        profile="unrestricted",
        working_dir=str(workspace),
        auto_approve=True,
    )
    agent = Agent(agent_config)

    # Register custom tools from workspace
    for tool in custom_tools:
        agent.registry.register(tool)

    return agent
