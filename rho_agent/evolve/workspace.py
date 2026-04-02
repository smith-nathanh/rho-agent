"""Workspace directory operations and dynamic tool loading."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..core.config import AgentConfig
from ..tools.base import ToolHandler


def create_workspace(run_dir: str, gen_id: str) -> Path:
    """Create an empty workspace directory for a generation."""
    workspace = Path(run_dir) / "workspaces" / gen_id
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(exist_ok=True)
    (workspace / "lib").mkdir(exist_ok=True)
    return workspace


def copy_workspace(src: Path, dest: Path) -> None:
    """Copy a parent workspace to a new location for mutation."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


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
