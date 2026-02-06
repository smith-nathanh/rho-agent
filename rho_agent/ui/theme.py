"""Centralized CLI theme tokens."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CliTheme:
    """Semantic Rich color tokens for CLI output."""

    primary: str = "#E6EDF3"
    secondary: str = "#56B6C2"
    muted: str = "#7F848E"
    accent: str = "#61AFEF"
    success: str = "#98C379"
    warning: str = "#E5C07B"
    error: str = "#E06C75"
    border: str = "#3E4451"
    prompt: str = "#56B6C2"
    tool_call: str = "#61AFEF"
    tool_result: str = "#ABB2BF"


THEME = CliTheme()
