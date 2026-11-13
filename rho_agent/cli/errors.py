"""CLI exception classes."""

from __future__ import annotations

from ..client.model import MissingApiKeyError

__all__ = [
    "CliUsageError",
    "InvalidModeError",
    "InvalidProfileError",
    "MissingApiKeyError",
    "PromptLoadError",
]


class CliUsageError(ValueError):
    """Base class for user-facing CLI configuration and usage errors."""


class InvalidProfileError(CliUsageError):
    """Raised when a capability profile cannot be loaded."""

    def __init__(self, details: str) -> None:
        super().__init__(
            f"Invalid profile: {details}. Use --profile readonly|developer|unrestricted or a "
            "valid YAML profile path."
        )


class InvalidModeError(CliUsageError):
    """Raised when mode option values are invalid."""

    def __init__(self, option: str, value: str, allowed: str) -> None:
        super().__init__(f"Invalid {option} '{value}'. Allowed values: {allowed}.")


class PromptLoadError(CliUsageError):
    """Raised when prompt files or prompt variables cannot be loaded."""

    def __init__(self, details: str) -> None:
        super().__init__(f"Prompt configuration error: {details}")
