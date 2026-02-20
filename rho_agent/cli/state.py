"""Shared CLI state: console, app, constants, settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.theme import Theme

from .theme import THEME

# Config directory for rho-agent data
CONFIG_DIR = Path.home() / ".config" / "rho-agent"
HISTORY_FILE = CONFIG_DIR / "history"
DEFAULT_PROMPT_FILE = CONFIG_DIR / "default.md"

# Built-in default prompt (ships with package)
BUILTIN_PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "default.md"

# Render assistant output as markdown in interactive TTY sessions
RENDER_MARKDOWN = os.getenv("RHO_AGENT_RENDER_MARKDOWN", "1").lower() not in (
    "0",
    "false",
    "no",
)

# Rich console for all output
console = Console()

MARKDOWN_THEME = Theme(
    {
        "markdown": THEME.primary,
        "markdown.paragraph": THEME.primary,
        "markdown.text": THEME.primary,
        "markdown.item": THEME.primary,
        "markdown.item.bullet": THEME.primary,
        "markdown.code": THEME.primary,
        "markdown.code_block": THEME.primary,
        "markdown.block_quote": THEME.muted,
        "markdown.h1": f"bold {THEME.primary}",
        "markdown.h2": f"bold {THEME.primary}",
        "markdown.h3": f"bold {THEME.primary}",
        "markdown.h4": f"bold {THEME.primary}",
        "markdown.h5": f"bold {THEME.primary}",
        "markdown.h6": f"bold {THEME.primary}",
        "markdown.link": THEME.accent,
        "markdown.em": f"italic {THEME.primary}",
        "markdown.strong": f"bold {THEME.primary}",
    }
)


@dataclass
class Settings:
    """Mutable CLI settings adjusted at startup."""

    tool_preview_lines: int = field(
        default_factory=lambda: int(os.getenv("RHO_AGENT_PREVIEW_LINES", "6"))
    )


settings = Settings()

# Typer app
app = typer.Typer(
    name="rho-agent",
    help="An agent harness and CLI with readonly and developer modes.",
    epilog=(
        "Examples:\n"
        "  rho-agent\n"
        '  rho-agent "What errors are in app.log?"\n'
        '  rho-agent --prompt "Investigate why CI is failing"\n'
        "  rho-agent --profile readonly\n"
        "  rho-agent --profile developer\n"
        "  rho-agent -r latest\n"
        "  rho-agent --system-prompt ./prompt.md --var env=prod"
    ),
    add_completion=False,
)
