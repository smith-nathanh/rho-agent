"""Load and parse markdown prompt files with YAML frontmatter."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PromptVariable:
    """A variable expected by a prompt."""

    name: str
    required: bool = False
    default: str | None = None


@dataclass
class Prompt:
    """A loaded prompt configuration."""

    description: str
    variables: list[PromptVariable]
    system_prompt: str
    initial_prompt: str | None = None


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split markdown content into frontmatter dict and body.

    Args:
        content: Markdown content with optional YAML frontmatter

    Returns:
        Tuple of (frontmatter_dict, body_text)
        If no frontmatter, returns ({}, content)
    """
    content = content.strip()

    # Check for frontmatter delimiter
    if not content.startswith("---"):
        return {}, content

    # Find the closing delimiter
    # Skip the first "---" and find the next one
    lines = content.split("\n")
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        # No closing delimiter, treat entire content as body
        return {}, content

    # Extract frontmatter and body
    frontmatter_lines = lines[1:end_idx]
    body_lines = lines[end_idx + 1 :]

    frontmatter_text = "\n".join(frontmatter_lines)
    body = "\n".join(body_lines).strip()

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter: {exc}") from exc

    if frontmatter is None:
        frontmatter = {}

    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    return frontmatter, body


def load_prompt(path: str | Path) -> Prompt:
    """Load a markdown prompt file.

    Args:
        path: Path to the markdown file

    Returns:
        Loaded Prompt object

    Raises:
        ValueError: If file not found or invalid format
        FileNotFoundError: If file doesn't exist
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ValueError(f"Failed to read prompt file: {exc}") from exc

    frontmatter, body = parse_frontmatter(content)

    if not body.strip():
        raise ValueError(f"Prompt file has no content (system prompt): {path}")

    # Parse variables from frontmatter
    variables: list[PromptVariable] = []
    raw_vars = frontmatter.get("variables", {})
    if isinstance(raw_vars, dict):
        for var_name, var_config in raw_vars.items():
            if isinstance(var_config, dict):
                variables.append(
                    PromptVariable(
                        name=var_name,
                        required=var_config.get("required", False),
                        default=var_config.get("default"),
                    )
                )
            else:
                # Simple format: variable_name: default_value
                variables.append(
                    PromptVariable(
                        name=var_name,
                        required=False,
                        default=str(var_config) if var_config is not None else None,
                    )
                )

    return Prompt(
        description=frontmatter.get("description", ""),
        variables=variables,
        system_prompt=body,
        initial_prompt=frontmatter.get("initial_prompt"),
    )
