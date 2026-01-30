"""Render prompts with Jinja2 templating."""

from typing import Any

from jinja2 import Environment, BaseLoader, TemplateSyntaxError, UndefinedError

from .loader import Prompt


def _create_jinja_env() -> Environment:
    """Create a Jinja2 environment with useful defaults."""
    env = Environment(
        loader=BaseLoader(),
        # Keep trailing newlines
        keep_trailing_newline=True,
        # Trim whitespace around blocks for cleaner output
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


# Shared environment instance
_jinja_env = _create_jinja_env()


def render_string(template_str: str, variables: dict[str, Any]) -> str:
    """Render a Jinja2 template string with variables.

    Supports full Jinja2 syntax including:
    - Variable substitution: {{ variable }}
    - Conditionals: {% if condition %}...{% endif %}
    - Loops: {% for item in list %}...{% endfor %}
    - Filters: {{ value | upper }}

    Args:
        template_str: Jinja2 template string
        variables: Dict of variable name -> value

    Returns:
        Rendered string

    Raises:
        ValueError: If template syntax is invalid or required variable is missing
    """
    try:
        template = _jinja_env.from_string(template_str)
        return template.render(**variables)
    except TemplateSyntaxError as e:
        raise ValueError(f"Template syntax error at line {e.lineno}: {e.message}") from e
    except UndefinedError as e:
        raise ValueError(f"Missing variable: {e}") from e


def prepare_prompt(
    prompt: Prompt,
    variables: dict[str, str],
) -> tuple[str, str | None]:
    """Prepare a prompt for use by resolving all variables.

    Args:
        prompt: Loaded Prompt object
        variables: User-provided variables

    Returns:
        Tuple of (system_prompt, initial_prompt)
        initial_prompt may be None if not specified in prompt

    Raises:
        ValueError: If required variables are missing
    """
    # Build full variable set with defaults
    full_vars: dict[str, Any] = {}

    for var in prompt.variables:
        if var.name in variables:
            full_vars[var.name] = variables[var.name]
        elif var.default is not None:
            full_vars[var.name] = var.default
        elif var.required:
            raise ValueError(f"Missing required variable: {var.name}")

    # Also include any extra variables passed that aren't in the prompt spec
    # (allows flexibility without updating prompt file)
    for key, value in variables.items():
        if key not in full_vars:
            full_vars[key] = value

    # Render prompts
    system_prompt = render_string(prompt.system_prompt, full_vars)

    initial_prompt = None
    if prompt.initial_prompt:
        initial_prompt = render_string(prompt.initial_prompt, full_vars)

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
