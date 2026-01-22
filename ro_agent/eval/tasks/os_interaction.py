"""OS Interaction task loader and data structures."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import BaseTask


@dataclass
class CheckScript:
    """A check script configuration for evaluation."""

    file: str  # Path to check script file (relative to scripts dir)
    args: list[str] = field(default_factory=list)  # Additional arguments


@dataclass
class EvaluationConfig:
    """Evaluation configuration for an OS task."""

    eval_type: str  # "match" or "check"
    match_answer: str | None = None  # For eval_type="match" (exact match)
    match_regex: str | None = None  # For eval_type="match" (regex match)
    match_strip: bool = True  # Whether to strip whitespace before matching
    check_scripts: list[CheckScript] = field(default_factory=list)
    example_script: dict[str, Any] | None = None  # For getting expected value


@dataclass
class OSTask(BaseTask):
    """An OS Interaction evaluation task."""

    image: str  # Docker image: "default", "packages", "ubuntu"
    init_code: str | None = None  # Inline init script
    init_file: str | None = None  # Path to init script file
    start_script: str | None = None  # Background process to start
    evaluation: EvaluationConfig = field(default_factory=lambda: EvaluationConfig("match"))
    labels: list[str] = field(default_factory=list)  # Task categories
    scripts_dir: str | None = None  # Path to scripts directory for this task
    source_file: str | None = None  # Source JSON file (for debugging/reruns)

    def get_prompt(self) -> str:
        """Get the prompt to send to the agent."""
        prompt = f"""{self.description}

Use bash_action to execute shell commands. When you have found the answer, use answer_action to submit it."""
        return prompt

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "index": self.index,
            "description": self.description,
            "image": self.image,
            "init_code": self.init_code,
            "init_file": self.init_file,
            "start_script": self.start_script,
            "eval_type": self.evaluation.eval_type,
            "labels": self.labels,
        }


def load_os_tasks(
    data_path: str | Path,
    scripts_dir: str | Path | None = None,
    start_index: int = 0,
) -> list[OSTask]:
    """Load OS Interaction tasks from a JSON file.

    Args:
        data_path: Path to the task data JSON file (e.g., dev.json)
        scripts_dir: Optional path to scripts directory for resolving init files
        start_index: Starting index for task numbering (for combining multiple files)

    Returns:
        List of OSTask objects
    """
    data_path = Path(data_path)
    scripts_dir_str = str(scripts_dir) if scripts_dir else None

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Handle both list format and dict format
    if isinstance(data, dict):
        tasks_data = data.get("tasks", data.get("data", [data]))
    else:
        tasks_data = data

    tasks = []
    for idx, task_data in enumerate(tasks_data):
        # Parse create config
        create = task_data.get("create", {})

        # Handle malformed create fields (list or null)
        if not isinstance(create, dict):
            create = {}

        # Determine image
        if "local" in create:
            image = create["local"]
        elif "docker" in create:
            image = create["docker"]
        else:
            image = "default"

        # Parse init config
        init_config = create.get("init", {})
        init_code = None
        init_file = None

        if isinstance(init_config, dict):
            init_code = init_config.get("code")
            init_file = init_config.get("file")
        elif isinstance(init_config, str):
            # Could be code or file path
            init_code = init_config

        # Parse evaluation config
        eval_data = task_data.get("evaluation", {})
        eval_config = parse_evaluation_config(eval_data)

        task = OSTask(
            index=start_index + idx,
            description=task_data.get("description", ""),
            image=image,
            init_code=init_code,
            init_file=init_file,
            start_script=task_data.get("start"),
            evaluation=eval_config,
            labels=task_data.get("labels", []),
            scripts_dir=scripts_dir_str,
            source_file=str(data_path),
        )
        tasks.append(task)

    return tasks


def load_os_benchmark(base_path: str | Path) -> list[OSTask]:
    """Load the full OS Interaction benchmark from the AgentBench directory structure.

    Expects the standard AgentBench layout:
        base_path/
            data/
                1/*.json
                2/*.json
                ...
                7/*.json
            scripts/
                1/
                2/
                ...
                7/

    Args:
        base_path: Path to the os_interaction directory

    Returns:
        List of OSTask objects from all task files
    """
    base_path = Path(base_path)
    data_dir = base_path / "data"
    scripts_base = base_path / "scripts"

    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    all_tasks: list[OSTask] = []
    task_index = 0

    # Load from numbered directories (1-7)
    for subdir_num in range(1, 8):
        subdir = data_dir / str(subdir_num)
        if not subdir.exists():
            continue

        scripts_dir = scripts_base / str(subdir_num)

        # Load all JSON files in this subdirectory
        for json_file in sorted(subdir.glob("*.json")):
            tasks = load_os_tasks(
                json_file,
                scripts_dir=scripts_dir if scripts_dir.exists() else None,
                start_index=task_index,
            )
            all_tasks.extend(tasks)
            task_index += len(tasks)

    return all_tasks


def parse_evaluation_config(eval_data: dict[str, Any]) -> EvaluationConfig:
    """Parse evaluation configuration from task data."""
    if not eval_data:
        return EvaluationConfig(eval_type="match")

    # Check for match-based evaluation
    if "match" in eval_data:
        match_data = eval_data["match"]
        # AgentBench: if string, converts to {"answer": str, "strip": True}
        if isinstance(match_data, str):
            return EvaluationConfig(
                eval_type="match",
                match_answer=match_data,
                match_strip=True,
            )
        # Dict format with answer, regex, strip options
        return EvaluationConfig(
            eval_type="match",
            match_answer=match_data.get("answer"),
            match_regex=match_data.get("regex"),
            match_strip=match_data.get("strip", True),
        )

    # Check for check-based evaluation
    if "check" in eval_data:
        check_data = eval_data["check"]
        check_scripts = []

        if isinstance(check_data, dict):
            # Single check script
            check_scripts.append(
                CheckScript(
                    file=check_data.get("file", ""),
                    args=check_data.get("args", []),
                )
            )
        elif isinstance(check_data, list):
            # Multiple check scripts or [null, script] format
            for item in check_data:
                if item is None:
                    continue
                if isinstance(item, dict):
                    check_scripts.append(
                        CheckScript(
                            file=item.get("file", ""),
                            args=item.get("args", []),
                        )
                    )
                elif isinstance(item, str):
                    check_scripts.append(CheckScript(file=item))

        # Parse example script if present
        # Can be a dict {"code": "..."} or a string (direct shell command)
        example = eval_data.get("example")
        example_script = None
        if isinstance(example, dict):
            example_script = example
        elif isinstance(example, str):
            # Convert string format to dict format
            example_script = {"code": example}

        return EvaluationConfig(
            eval_type="check",
            check_scripts=check_scripts,
            example_script=example_script,
        )

    return EvaluationConfig(eval_type="match")
