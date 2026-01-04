"""Load and validate template YAML files."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Default directories for templates and layouts
TEMPLATES_DIR = Path.home() / ".config" / "ro-agent" / "templates"
LAYOUTS_DIR = Path.home() / ".config" / "ro-agent" / "layouts"


@dataclass
class TemplateVariable:
    """A variable expected by a template."""

    name: str
    description: str = ""
    required: bool = False
    default: str | None = None


@dataclass
class ToolHints:
    """Optional hints about which tools to prefer or avoid."""

    prefer: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)


@dataclass
class Template:
    """A loaded template configuration."""

    name: str
    description: str
    variables: list[TemplateVariable]
    system_prompt: str
    initial_prompt: str | None = None
    repo_layout: str | None = None  # Layout name to load
    tool_hints: ToolHints | None = None


def load_template(
    name: str,
    templates_dir: Path = TEMPLATES_DIR,
) -> Template:
    """Load a template by name from the templates directory.

    Args:
        name: Template name (without .yaml extension)
        templates_dir: Directory to search for templates

    Returns:
        Loaded Template object

    Raises:
        ValueError: If template not found or invalid
    """
    path = templates_dir / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"Template not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        raise ValueError(f"Failed to parse template {name}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Template {name} must be a YAML mapping")

    # Validate required fields
    if "name" not in data:
        raise ValueError(f"Template {name} missing required field: name")
    if "system_prompt" not in data:
        raise ValueError(f"Template {name} missing required field: system_prompt")

    # Parse variables
    variables: list[TemplateVariable] = []
    raw_vars = data.get("variables", {})
    if isinstance(raw_vars, dict):
        for var_name, var_config in raw_vars.items():
            if isinstance(var_config, dict):
                variables.append(
                    TemplateVariable(
                        name=var_name,
                        description=var_config.get("description", ""),
                        required=var_config.get("required", False),
                        default=var_config.get("default"),
                    )
                )
            else:
                # Simple format: variable_name: default_value
                variables.append(
                    TemplateVariable(
                        name=var_name,
                        description="",
                        required=False,
                        default=str(var_config) if var_config is not None else None,
                    )
                )

    # Parse tool hints
    tool_hints = None
    raw_hints = data.get("tool_hints")
    if isinstance(raw_hints, dict):
        tool_hints = ToolHints(
            prefer=raw_hints.get("prefer", []),
            avoid=raw_hints.get("avoid", []),
        )

    return Template(
        name=data["name"],
        description=data.get("description", ""),
        variables=variables,
        system_prompt=data["system_prompt"],
        initial_prompt=data.get("initial_prompt"),
        repo_layout=data.get("repo_layout"),
        tool_hints=tool_hints,
    )


def load_layout(
    name: str,
    layouts_dir: Path = LAYOUTS_DIR,
) -> dict:
    """Load a layout file by name.

    Args:
        name: Layout name (without .yaml extension)
        layouts_dir: Directory to search for layouts

    Returns:
        Raw layout data as dict

    Raises:
        ValueError: If layout not found or invalid
    """
    path = layouts_dir / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"Layout not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        raise ValueError(f"Failed to parse layout {name}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Layout {name} must be a YAML mapping")

    return data


def list_templates(templates_dir: Path = TEMPLATES_DIR) -> list[str]:
    """List available template names.

    Args:
        templates_dir: Directory to search for templates

    Returns:
        List of template names (without .yaml extension)
    """
    if not templates_dir.exists():
        return []

    return sorted(
        p.stem for p in templates_dir.glob("*.yaml") if p.is_file()
    )


def list_layouts(layouts_dir: Path = LAYOUTS_DIR) -> list[str]:
    """List available layout names.

    Args:
        layouts_dir: Directory to search for layouts

    Returns:
        List of layout names (without .yaml extension)
    """
    if not layouts_dir.exists():
        return []

    return sorted(
        p.stem for p in layouts_dir.glob("*.yaml") if p.is_file()
    )
