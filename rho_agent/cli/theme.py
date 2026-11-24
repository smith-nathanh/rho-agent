"""Centralized CLI theme tokens, status metadata, and prompt_toolkit styling.

Default palette ("stone") pairs warm matte charcoal surfaces with a single
muted terracotta accent. Users can switch themes via ``RHO_AGENT_THEME``,
``--theme``, ``/theme`` (CLI), or ``theme`` (monitor).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.styles import Style as PtkStyle

THEME_CONFIG_FILE = Path.home() / ".config" / "rho-agent" / "theme"
DEFAULT_THEME_NAME = "stone"


@dataclass(frozen=True)
class CliTheme:
    """Semantic Rich color tokens for CLI and monitor output."""

    name: str = "stone"
    description: str = "Warm matte charcoal with muted terracotta accent"

    # Neutral typography & architectural hairlines
    primary: str = "#E6E2DC"  # warm off-white body copy
    secondary: str = "#A8A29E"  # stone secondary text
    brand: str = "#F5F2ED"  # bright warm-white wordmark
    muted: str = "#999087"  # quiet warm graphite metadata
    subtle: str = "#9C958F"  # secondary body copy
    border: str = "#494039"  # whisper-thin hairline border
    border_active: str = "#63574D"  # focused hairline border

    # Focal mark, accent, and restrained semantic states
    mark: str = "#D97757"  # rho brand mark & user prompt bar (unified with accent)
    accent: str = "#D97757"  # muted terracotta focal accent
    code: str = "#E6B8A2"  # warm sandstone inline code foreground (no bg box)
    success: str = "#6FC297"  # muted sage
    warning: str = "#E5B472"  # warm sand-amber (separated from terracotta accent)
    error: str = "#E07A7A"  # dusty coral red
    subagent: str = "#A8A29E"  # neutral hierarchy (tree lines carry structure)

    # Interactive & tools
    prompt: str = "#D97757"
    tool_call: str = "#D6D1CA"
    tool_name: str = "#E6E2DC"
    tool_result: str = "#9C958F"
    rail: str = "#494039"

    # Editorial diffs
    diff_add: str = "#6FC297"
    diff_del: str = "#E07A7A"
    diff_add_bg: str = "#15231C"
    diff_del_bg: str = "#271717"
    diff_hunk: str = "#999087"

    # Matte neutral surfaces
    user_bg: str = "#1E1C1A"
    code_bg: str = "#23201E"
    toolbar_bg: str = "#181715"
    toolbar_fg: str = "#9C958F"
    toolbar_muted: str = "#8D857E"
    selection_bg: str = "#463A32"
    selection_fg: str = "#F5F2ED"


STONE_THEME = CliTheme(name="stone")

TEAL_THEME = CliTheme(
    name="teal",
    description="Matte graphite with calm sea-glass teal accent",
    primary="#E4E9EA",
    secondary="#97A3A6",
    brand="#F4F6F6",
    muted="#87979B",
    subtle="#91A0A3",
    border="#3A474B",
    border_active="#526167",
    mark="#52B8B2",
    accent="#52B8B2",
    code="#9AD4D0",
    success="#74CC8A",
    warning="#E2AE6D",
    error="#E57A7A",
    subagent="#97A3A6",
    prompt="#52B8B2",
    tool_call="#D1D8DA",
    tool_name="#E4E9EA",
    tool_result="#91A0A3",
    rail="#3A474B",
    diff_add="#74CC8A",
    diff_del="#E57A7A",
    diff_add_bg="#13241B",
    diff_del_bg="#271718",
    diff_hunk="#87979B",
    user_bg="#1A1E20",
    code_bg="#202427",
    toolbar_bg="#15181A",
    toolbar_fg="#97A3A6",
    toolbar_muted="#849297",
    selection_bg="#35474A",
    selection_fg="#F4F6F6",
)

DUSK_THEME = CliTheme(
    name="dusk",
    description="Midnight slate with soft periwinkle-violet accent",
    primary="#E4E6F0",
    secondary="#989DB2",
    brand="#F4F5FA",
    muted="#8F95AD",
    subtle="#969BB1",
    border="#3D4256",
    border_active="#565C77",
    mark="#9E95F0",
    accent="#9E95F0",
    code="#C6C0F5",
    success="#68CCA0",
    warning="#E3B06E",
    error="#E87A86",
    subagent="#989DB2",
    prompt="#9E95F0",
    tool_call="#CFD3E4",
    tool_name="#E4E6F0",
    tool_result="#969BB1",
    rail="#3D4256",
    diff_add="#68CCA0",
    diff_del="#E87A86",
    diff_add_bg="#12241E",
    diff_del_bg="#28161B",
    diff_hunk="#8F95AD",
    user_bg="#1A1C26",
    code_bg="#202330",
    toolbar_bg="#15161E",
    toolbar_fg="#989DB2",
    toolbar_muted="#898FA7",
    selection_bg="#3B405B",
    selection_fg="#F4F5FA",
)

OBSIDIAN_THEME = CliTheme(
    name="obsidian",
    description="Pure architectural monochrome with crisp white accent",
    primary="#D8D8DE",
    secondary="#9E9EA8",
    brand="#FAFAFA",
    muted="#94949E",
    subtle="#9C9CA6",
    border="#42424B",
    border_active="#5B5B67",
    mark="#FAFAFA",
    accent="#FAFAFA",
    code="#FFFFFF",
    success="#5AD28E",
    warning="#E2AC6B",
    error="#EE7878",
    subagent="#9E9EA8",
    prompt="#FAFAFA",
    tool_call="#D0D0D8",
    tool_name="#E4E4EA",
    tool_result="#9C9CA6",
    rail="#42424B",
    diff_add="#5AD28E",
    diff_del="#EE7878",
    diff_add_bg="#102218",
    diff_del_bg="#261316",
    diff_hunk="#94949E",
    user_bg="#1B1B1F",
    code_bg="#1F1F24",
    toolbar_bg="#161619",
    toolbar_fg="#9E9EA8",
    toolbar_muted="#898993",
    selection_bg="#40404A",
    selection_fg="#FAFAFA",
)

GLACIER_THEME = CliTheme(
    name="glacier",
    description="Deep navy with ice-blue accents and warm honey code",
    primary="#E7F3FA",
    secondary="#9DB4C5",
    brand="#F3FAFF",
    muted="#8EA7BA",
    subtle="#9DB4C5",
    border="#3C5266",
    border_active="#57758D",
    mark="#78C8FF",
    accent="#78C8FF",
    code="#F1CB8F",
    success="#7AD8AE",
    warning="#FFBF79",
    error="#FF969D",
    subagent="#9DB4C5",
    prompt="#78C8FF",
    tool_call="#C4D7E4",
    tool_name="#E7F3FA",
    tool_result="#9DB4C5",
    rail="#3C5266",
    diff_add="#7AD8AE",
    diff_del="#FF969D",
    diff_add_bg="#142B27",
    diff_del_bg="#321C29",
    diff_hunk="#8EA7BA",
    user_bg="#172738",
    code_bg="#192A3A",
    toolbar_bg="#101822",
    toolbar_fg="#9DB4C5",
    toolbar_muted="#8EA7BA",
    selection_bg="#2A4763",
    selection_fg="#F3FAFF",
)

MOSS_THEME = CliTheme(
    name="moss",
    description="Olive charcoal with yellow-green accents and pale gold code",
    primary="#F0F1E1",
    secondary="#B2B59D",
    brand="#F9F9EE",
    muted="#A0A58F",
    subtle="#B2B59D",
    border="#4A513B",
    border_active="#636E4F",
    mark="#C4D882",
    accent="#C4D882",
    code="#EDD397",
    success="#81D4AA",
    warning="#FFC47F",
    error="#FF9891",
    subagent="#B2B59D",
    prompt="#C4D882",
    tool_call="#D1D4BF",
    tool_name="#F0F1E1",
    tool_result="#B2B59D",
    rail="#4A513B",
    diff_add="#81D4AA",
    diff_del="#FF9891",
    diff_add_bg="#1B3023",
    diff_del_bg="#382321",
    diff_hunk="#A0A58F",
    user_bg="#282F20",
    code_bg="#2C3223",
    toolbar_bg="#181B13",
    toolbar_fg="#B2B59D",
    toolbar_muted="#A0A58F",
    selection_bg="#3D482A",
    selection_fg="#F9F9EE",
)

LIGHT_THEME = CliTheme(
    name="light",
    description="Paper-minimal terracotta (for light-background terminals)",
    primary="#1C1917",
    secondary="#57534E",
    brand="#0C0A09",
    muted="#6D645D",
    subtle="#57534E",
    border="#D6D3D1",
    border_active="#A8A29E",
    mark="#A95032",
    accent="#A95032",
    code="#9A3F24",
    success="#047857",
    warning="#B45309",
    error="#C92C2C",
    subagent="#57534E",
    prompt="#A95032",
    tool_call="#292524",
    tool_name="#1C1917",
    tool_result="#57534E",
    rail="#D6D3D1",
    diff_add="#047857",
    diff_del="#B91C1C",
    diff_add_bg="#ECFDF5",
    diff_del_bg="#FEF2F2",
    diff_hunk="#6D645D",
    user_bg="#EFECE8",
    code_bg="#F0EDE9",
    toolbar_bg="#F5F3F0",
    toolbar_fg="#57534E",
    toolbar_muted="#6D645D",
    selection_bg="#E0DCD6",
    selection_fg="#0C0A09",
)

THEMES: dict[str, CliTheme] = {
    "stone": STONE_THEME,
    "teal": TEAL_THEME,
    "dusk": DUSK_THEME,
    "obsidian": OBSIDIAN_THEME,
    "glacier": GLACIER_THEME,
    "moss": MOSS_THEME,
    "light": LIGHT_THEME,
}

THEME_ALIASES: dict[str, str] = {
    "terracotta": "stone",
    "sage": "teal",
    "slate": "dusk",
    "mono": "obsidian",
    "carbon": "obsidian",
}


def resolve_theme_name(name: str) -> str | None:
    """Resolve a theme name or alias to its canonical key in ``THEMES``."""
    key = name.strip().lower()
    if key in THEMES:
        return key
    canonical = THEME_ALIASES.get(key)
    if canonical and canonical in THEMES:
        return canonical
    return None


def read_saved_theme_name(config_file: Path | None = None) -> str | None:
    """Read persisted theme preference from ``~/.config/rho-agent/theme``."""
    path = config_file or THEME_CONFIG_FILE
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return resolve_theme_name(raw) if raw else None


def save_theme_name(name: str, config_file: Path | None = None) -> None:
    """Persist the user's selected theme name to ``~/.config/rho-agent/theme``."""
    canonical = resolve_theme_name(name)
    if canonical is None:
        raise ValueError(f"Unknown theme: {name}")
    path = config_file or THEME_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{canonical}\n", encoding="utf-8")


def load_theme(config_file: Path | None = None) -> CliTheme:
    """Resolve active theme from ``RHO_AGENT_THEME``, saved preference, or default."""
    env_choice = os.getenv("RHO_AGENT_THEME", "").strip()
    if env_choice:
        resolved = resolve_theme_name(env_choice)
        if resolved is not None:
            return THEMES[resolved]
    saved = read_saved_theme_name(config_file)
    if saved is not None:
        return THEMES[saved]
    return THEMES[DEFAULT_THEME_NAME]


class ActiveTheme:
    """Mutable proxy delegating attribute access to the currently active ``CliTheme``.

    Because modules import ``THEME`` once, updating ``THEME`` in place lets
    ``/theme`` and ``--theme`` switch palettes live across the entire CLI and
    monitor without restarting.
    """

    def __init__(self, initial: CliTheme) -> None:
        self._current = initial

    @property
    def current(self) -> CliTheme:
        return self._current

    def apply(self, theme: CliTheme) -> None:
        self._current = theme

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current, name)

    def __repr__(self) -> str:
        return f"ActiveTheme({self._current!r})"


THEME = ActiveTheme(load_theme())

_THEME_LISTENERS: list[Callable[[CliTheme], None]] = []
_CACHED_PTK_STYLE: PtkStyle | None = None


def on_theme_change(callback: Callable[[CliTheme], None]) -> None:
    """Register a listener invoked whenever :func:`set_theme` changes the active theme."""
    if callback not in _THEME_LISTENERS:
        _THEME_LISTENERS.append(callback)


def set_theme(
    name: str,
    *,
    persist: bool = False,
    config_file: Path | None = None,
) -> CliTheme:
    """Switch the active CLI/monitor theme in place and notify listeners."""
    global _CACHED_PTK_STYLE
    canonical = resolve_theme_name(name)
    if canonical is None:
        available = ", ".join(THEMES)
        raise ValueError(f"Unknown theme '{name}'. Available: {available}")
    new_theme = THEMES[canonical]
    THEME.apply(new_theme)
    _CACHED_PTK_STYLE = None
    if persist:
        save_theme_name(canonical, config_file=config_file)
    for listener in list(_THEME_LISTENERS):
        listener(new_theme)
    return new_theme


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


def status_style(status: str, theme: CliTheme | ActiveTheme = THEME) -> tuple[str, str]:
    """Return ``(glyph, hex_color)`` for a session status."""
    meta = STATUS_STYLES.get(status)
    if meta is None:
        return "·", theme.muted
    return meta.glyph, getattr(theme, meta.color_attr)


def tool_visual(tool_name: str | None, theme: CliTheme | ActiveTheme = THEME) -> tuple[str, str]:
    """Return ``(marker, hex_color)`` for a tool name — clean and minimal."""
    if not tool_name:
        return "·", theme.primary
    if tool_name == "request_access":
        return "·", theme.primary
    if tool_name in ("delegate", "agent"):
        return "↳", theme.accent
    return "·", theme.primary


def build_ptk_style(theme: CliTheme | ActiveTheme = THEME) -> PtkStyle:
    """Build a minimal matte prompt_toolkit style matching ``theme``."""
    return PtkStyle.from_dict(
        {
            # Prompt tokens
            "prompt.arrow": f"bold {theme.prompt}",
            "prompt.mark": f"bold {theme.mark}",
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
            "toolbar.ok": f"bg:{theme.toolbar_bg} {theme.primary} noreverse",
            "toolbar.warn": f"bg:{theme.toolbar_bg} {theme.warning} noreverse",
            "toolbar.err": f"bg:{theme.toolbar_bg} {theme.error} noreverse",
            "toolbar.accent": f"bg:{theme.toolbar_bg} {theme.accent} noreverse",
            # Minimal completion dropdown
            "completion-menu": f"bg:{theme.toolbar_bg} {theme.toolbar_fg}",
            "completion-menu.completion": f"bg:{theme.toolbar_bg} {theme.primary}",
            "completion-menu.completion.current": f"bg:{theme.selection_bg} {theme.selection_fg}",
            "completion-menu.meta.completion": f"bg:{theme.toolbar_bg} {theme.muted}",
            "completion-menu.meta.completion.current": (
                f"bg:{theme.selection_bg} {theme.selection_fg}"
            ),
            "scrollbar.background": f"bg:{theme.toolbar_bg}",
            "scrollbar.button": f"bg:{theme.border}",
        }
    )


def _active_ptk_style() -> PtkStyle:
    global _CACHED_PTK_STYLE
    if _CACHED_PTK_STYLE is None:
        _CACHED_PTK_STYLE = build_ptk_style(THEME)
    return _CACHED_PTK_STYLE


PTK_STYLE = DynamicStyle(_active_ptk_style)
