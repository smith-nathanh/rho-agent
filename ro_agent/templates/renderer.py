"""Render templates with variable substitution."""

import re
from pathlib import Path
from typing import Any

from .loader import LAYOUTS_DIR, Template, load_layout


def render_string(template_str: str, variables: dict[str, Any]) -> str:
    """Substitute {{ variable }} placeholders in a string.

    Args:
        template_str: String with {{ var }} placeholders
        variables: Dict of variable name -> value

    Returns:
        String with placeholders replaced

    Raises:
        ValueError: If a required variable is missing
    """

    def replace(match: re.Match) -> str:
        var_name = match.group(1).strip()
        if var_name not in variables:
            raise ValueError(f"Missing variable: {var_name}")
        return str(variables[var_name])

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, template_str)


def format_layout(layout_data: dict) -> str:
    """Format a layout dict into markdown for injection into system prompt.

    Args:
        layout_data: Raw layout data from YAML

    Returns:
        Formatted markdown string
    """
    sections: list[str] = []

    # Directory structure
    if structure := layout_data.get("structure"):
        sections.append(f"### Directory Structure\n```\n{structure.strip()}\n```")

    # Key paths
    if key_paths := layout_data.get("key_paths"):
        lines = ["### Key Paths"]
        for name, path in key_paths.items():
            lines.append(f"- **{name}**: `{path}`")
        sections.append("\n".join(lines))

    # Error patterns
    if error_patterns := layout_data.get("error_patterns"):
        lines = ["### Known Error Patterns"]
        for ep in error_patterns:
            pattern = ep.get("pattern", "")
            lines.append(f"\n**Pattern**: `{pattern}`")
            if cause := ep.get("likely_cause"):
                lines.append(f"- Likely cause: {cause}")
            if look_in := ep.get("look_in"):
                if isinstance(look_in, list):
                    lines.append(f"- Look in: {', '.join(look_in)}")
                else:
                    lines.append(f"- Look in: {look_in}")
            if hint := ep.get("investigation_hint"):
                lines.append(f"- Hint: {hint}")
        sections.append("\n".join(lines))

    # Cluster context
    if cluster_context := layout_data.get("cluster_context"):
        sections.append(f"### Cluster Context\n{cluster_context.strip()}")

    return "\n\n".join(sections)


def prepare_template(
    template: Template,
    variables: dict[str, str],
    layouts_dir: Path = LAYOUTS_DIR,
) -> tuple[str, str | None]:
    """Prepare a template for use by resolving all variables and layouts.

    Args:
        template: Loaded Template object
        variables: User-provided variables
        layouts_dir: Directory to load layouts from

    Returns:
        Tuple of (system_prompt, initial_prompt)
        initial_prompt may be None if not specified in template

    Raises:
        ValueError: If required variables are missing
    """
    # Build full variable set with defaults
    full_vars: dict[str, Any] = {}

    for var in template.variables:
        if var.name in variables:
            full_vars[var.name] = variables[var.name]
        elif var.default is not None:
            full_vars[var.name] = var.default
        elif var.required:
            raise ValueError(f"Missing required variable: {var.name}")

    # Also include any extra variables passed that aren't in the template spec
    # (allows flexibility without updating template)
    for key, value in variables.items():
        if key not in full_vars:
            full_vars[key] = value

    # Load and format repo layout if specified
    if template.repo_layout:
        try:
            layout_name = render_string(template.repo_layout, full_vars)
            layout_data = load_layout(layout_name, layouts_dir)
            full_vars["repo_layout"] = format_layout(layout_data)
        except ValueError as exc:
            # If layout not found, include error message as placeholder
            full_vars["repo_layout"] = f"[Layout error: {exc}]"

    # Render prompts
    system_prompt = render_string(template.system_prompt, full_vars)

    initial_prompt = None
    if template.initial_prompt:
        initial_prompt = render_string(template.initial_prompt, full_vars)

    return system_prompt, initial_prompt


def parse_var_string(var_string: str) -> tuple[str, str]:
    """Parse a 'key=value' string into (key, value).

    Args:
        var_string: String in format "key=value"

    Returns:
        Tuple of (key, value)

    Raises:
        ValueError: If string is not in key=value format
    """
    if "=" not in var_string:
        raise ValueError(f"Invalid variable format (expected key=value): {var_string}")

    key, value = var_string.split("=", 1)
    key = key.strip()
    value = value.strip()

    if not key:
        raise ValueError(f"Empty variable name in: {var_string}")

    return key, value


def parse_vars(var_list: list[str]) -> dict[str, str]:
    """Parse a list of 'key=value' strings into a dict.

    Args:
        var_list: List of strings in format "key=value"

    Returns:
        Dict of variable name -> value

    Raises:
        ValueError: If any string is not in key=value format
    """
    result: dict[str, str] = {}
    for var_string in var_list:
        key, value = parse_var_string(var_string)
        result[key] = value
    return result
