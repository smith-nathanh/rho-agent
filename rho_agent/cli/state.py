"""Shared CLI state: console, app, constants, settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.theme import Theme

from .theme import THEME, ActiveTheme, CliTheme, on_theme_change

# Config directory for rho-agent data
CONFIG_DIR = Path.home() / ".config" / "rho-agent"
HISTORY_FILE = CONFIG_DIR / "history"
MONITOR_HISTORY_FILE = CONFIG_DIR / "monitor_history"
SESSIONS_DIR = CONFIG_DIR / "sessions"
DEFAULT_PROMPT_FILE = CONFIG_DIR / "default.md"

# Built-in default prompt (ships with package)
BUILTIN_PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "default.md"

# Render assistant output as markdown in interactive TTY sessions
RENDER_MARKDOWN = os.getenv("RHO_AGENT_RENDER_MARKDOWN", "1").lower() not in (
    "0",
    "false",
    "no",
)


def build_markdown_theme(theme: CliTheme | ActiveTheme = THEME) -> Theme:
    """Build a Rich Markdown Theme matching ``theme``."""
    return Theme(
        {
            "markdown": theme.primary,
            "markdown.paragraph": theme.primary,
            "markdown.text": theme.primary,
            "markdown.item": theme.primary,
            "markdown.item.bullet": theme.muted,
            "markdown.item.number": theme.muted,
            "markdown.code": theme.code,
            "markdown.code_block": theme.primary,
            "markdown.block_quote": theme.subtle,
            "markdown.h1": f"bold {theme.brand}",
            "markdown.h2": f"bold {theme.primary}",
            "markdown.h3": f"bold {theme.secondary}",
            "markdown.h4": theme.secondary,
            "markdown.h5": theme.subtle,
            "markdown.h6": theme.muted,
            "markdown.hr": theme.border,
            "markdown.link": f"underline {theme.accent}",
            "markdown.link_url": theme.muted,
            "markdown.em": f"italic {theme.primary}",
            "markdown.strong": f"bold {theme.brand}",
        }
    )


MARKDOWN_THEME = build_markdown_theme(THEME)

# Rich console for all output (pushed theme lets /theme swap palettes live)
console = Console()
console.push_theme(MARKDOWN_THEME)


def _refresh_console_theme(theme: CliTheme) -> None:
    global MARKDOWN_THEME
    MARKDOWN_THEME = build_markdown_theme(theme)
    console.pop_theme()
    console.push_theme(MARKDOWN_THEME)


on_theme_change(_refresh_console_theme)


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
    help="An agent harness and CLI. Sandboxed by default: edits the working directory, asks before going outside it.",
    epilog=(
        "Examples:\n"
        "  rho-agent\n"
        '  rho-agent "What errors are in app.log?"\n'
        '  rho-agent --prompt "Investigate why CI is failing"\n'
        "  rho-agent --access read-only\n"
        "  rho-agent --add-dir ../shared-lib\n"
        "  rho-agent -r latest\n"
        "  rho-agent monitor --live\n"
        "  rho-agent --system-prompt ./prompt.md --var env=prod"
    ),
    add_completion=False,
)
