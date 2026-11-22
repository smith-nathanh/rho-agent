"""Centralized CLI theme tokens, status metadata, and prompt_toolkit styling.

Default palette ("carbon") is a modern, minimal monochrome dark UI with thin
architectural hairlines and a single calm blue accent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from prompt_toolkit.styles import Style as PtkStyle


@dataclass(frozen=True)
class CliTheme:
    """Semantic Rich color tokens for CLI and monitor output."""

    name: str = "carbon"

    # Neutral monochrome typography & hairlines
    primary: str = "#EAEAEA"  # crisp off-white
    secondary: str = "#A3A3A3"  # calm neutral silver
    brand: str = "#FAFAFA"  # pure white wordmark
    muted: str = "#666666"  # quiet graphite metadata
    subtle: str = "#8E8E8E"  # secondary body copy
    border: str = "#333333"  # whisper-thin hairline border
    border_active: str = "#525252"  # focused hairline border

    # Single surgical accent + restrained semantic states
    accent: str = "#60A5FA"  # calm cobalt blue focal accent
    success: str = "#6EE7B7"  # muted sage
    warning: str = "#E5B869"  # warm sand
    error: str = "#F87171"  # soft coral red
    subagent: str = "#A3A3A3"  # neutral hierarchy (tree lines carry structure)

    # Interactive & tools
    prompt: str = "#60A5FA"
    tool_call: str = "#D4D4D4"
    tool_name: str = "#EAEAEA"
    tool_result: str = "#8E8E8E"
    rail: str = "#333333"

    # Editorial diffs
    diff_add: str = "#6EE7B7"
    diff_del: str = "#F87171"
    diff_add_bg: str = "#13221B"
    diff_del_bg: str = "#251618"
    diff_hunk: str = "#555555"

    # Matte neutral surfaces
    user_bg: str = "#1E1E1E"
    code_bg: str = "#222222"
    toolbar_bg: str = "#171717"
    toolbar_fg: str = "#8E8E8E"
    toolbar_muted: str = "#555555"
    selection_bg: str = "#242424"
    selection_fg: str = "#FAFAFA"


CARBON_THEME = CliTheme(name="carbon")

# Clean paper-minimal light terminal theme (set RHO_AGENT_THEME=light)
LIGHT_THEME = CliTheme(
    name="light",
    primary="#171717",
    secondary="#525252",
    brand="#0A0A0A",
    muted="#737373",
    subtle="#525252",
    border="#D4D4D4",
    border_active="#737373",
    accent="#2563EB",
    success="#059669",
    warning="#B45309",
    error="#DC2626",
    subagent="#525252",
    prompt="#2563EB",
    tool_call="#262626",
    tool_name="#171717",
    tool_result="#525252",
    rail="#D4D4D4",
    diff_add="#047857",
    diff_del="#B91C1C",
    diff_add_bg="#ECFDF5",
    diff_del_bg="#FEF2F2",
    diff_hunk="#737373",
    user_bg="#EFEFEF",
    code_bg="#F0F0F0",
    toolbar_bg="#F5F5F5",
    toolbar_fg="#525252",
    toolbar_muted="#737373",
    selection_bg="#E5E5E5",
    selection_fg="#0A0A0A",
)

THEMES: dict[str, CliTheme] = {
    "carbon": CARBON_THEME,
    "light": LIGHT_THEME,
}


def load_theme() -> CliTheme:
    """Resolve active theme from ``RHO_AGENT_THEME`` (default: carbon)."""
    choice = os.getenv("RHO_AGENT_THEME", "carbon").strip().lower()
    return THEMES.get(choice, CARBON_THEME)


THEME = load_theme()


@dataclass(frozen=True)
class StatusStyle:
    """Minimal geometric indicator for a session lifecycle status."""

    glyph: str
    label: str
    color_attr: str  # attribute name on CliTheme


STATUS_STYLES: dict[str, StatusStyle] = {
    "running": StatusStyle("●", "running", "accent"),
    "waiting_input": StatusStyle("○", "waiting_input", "secondary"),
    "waiting_approval": StatusStyle("●", "waiting_approval", "warning"),
    "paused": StatusStyle("▪", "paused", "muted"),
    "created": StatusStyle("○", "created", "muted"),
    "completed": StatusStyle("·", "completed", "muted"),
    "cancelled": StatusStyle("·", "cancelled", "muted"),
    "error": StatusStyle("×", "error", "error"),
    "dead": StatusStyle("×", "dead", "error"),
    "interrupted": StatusStyle("▪", "interrupted", "warning"),
    "max_turns": StatusStyle("·", "max_turns", "warning"),
    "budget_exceeded": StatusStyle("·", "budget_exceeded", "warning"),
    "unknown": StatusStyle("?", "unknown", "muted"),
}


def status_style(status: str, theme: CliTheme = THEME) -> tuple[str, str]:
    """Return ``(glyph, hex_color)`` for a session status."""
    meta = STATUS_STYLES.get(status)
    if meta is None:
        return "·", theme.muted
    return meta.glyph, getattr(theme, meta.color_attr)


def tool_visual(tool_name: str | None, theme: CliTheme = THEME) -> tuple[str, str]:
    """Return ``(marker, hex_color)`` for a tool name — clean and minimal."""
    if not tool_name:
        return "·", theme.primary
    if tool_name == "request_access":
        return "·", theme.warning
    if tool_name in ("delegate", "agent"):
        return "↳", theme.accent
    return "·", theme.primary


def build_ptk_style(theme: CliTheme = THEME) -> PtkStyle:
    """Build a minimal matte prompt_toolkit style matching ``theme``."""
    return PtkStyle.from_dict(
        {
            # Prompt tokens
            "prompt.arrow": f"bold {theme.prompt}",
            "prompt.readonly": theme.secondary,
            "prompt.full": theme.warning,
            "prompt.monitor": theme.primary,
            "prompt.badge.live": theme.accent,
            "prompt.badge.alert": theme.warning,
            "prompt.muted": theme.muted,
            "continuation": theme.border_active,
            # Quiet matte bottom bar
            "bottom-toolbar": f"bg:{theme.toolbar_bg} {theme.toolbar_fg} noreverse",
            "bottom-toolbar.text": f"bg:{theme.toolbar_bg} {theme.toolbar_fg} noreverse",
            "toolbar.pill.workspace": f"bg:{theme.toolbar_bg} {theme.primary} noreverse",
            "toolbar.pill.readonly": f"bg:{theme.toolbar_bg} {theme.secondary} noreverse",
            "toolbar.pill.full": f"bg:{theme.toolbar_bg} {theme.warning} noreverse",
            "toolbar.model": f"bg:{theme.toolbar_bg} {theme.secondary} noreverse",
            "toolbar.sep": f"bg:{theme.toolbar_bg} {theme.border} noreverse",
            "toolbar.muted": f"bg:{theme.toolbar_bg} {theme.toolbar_muted} noreverse",
            "toolbar.ok": f"bg:{theme.toolbar_bg} {theme.secondary} noreverse",
            "toolbar.warn": f"bg:{theme.toolbar_bg} {theme.warning} noreverse",
            "toolbar.err": f"bg:{theme.toolbar_bg} {theme.error} noreverse",
            "toolbar.accent": f"bg:{theme.toolbar_bg} {theme.accent} noreverse",
            # Minimal completion dropdown
            "completion-menu": f"bg:{theme.toolbar_bg} {theme.toolbar_fg}",
            "completion-menu.completion": f"bg:{theme.toolbar_bg} {theme.primary}",
            "completion-menu.completion.current": (f"bg:{theme.selection_bg} {theme.selection_fg}"),
            "completion-menu.meta.completion": f"bg:{theme.toolbar_bg} {theme.muted}",
            "completion-menu.meta.completion.current": (
                f"bg:{theme.selection_bg} {theme.secondary}"
            ),
            "scrollbar.background": f"bg:{theme.toolbar_bg}",
            "scrollbar.button": f"bg:{theme.border}",
        }
    )


PTK_STYLE = build_ptk_style(THEME)
