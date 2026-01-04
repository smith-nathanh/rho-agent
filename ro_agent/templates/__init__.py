"""Template system for task-specific prompt configuration."""

from .loader import Template, TemplateVariable, load_template
from .renderer import prepare_template, render_string

__all__ = [
    "Template",
    "TemplateVariable",
    "load_template",
    "prepare_template",
    "render_string",
]
