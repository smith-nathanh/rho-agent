"""User-facing CLI error types with actionable messages."""


class CliUsageError(ValueError):
    """Base class for user-facing CLI configuration and usage errors."""


class MissingApiKeyError(CliUsageError):
    """Raised when API credentials are required but missing."""

    def __init__(self) -> None:
        super().__init__(
            "Missing API key. Set OPENAI_API_KEY, or pass --base-url to an endpoint "
            "that does not require OpenAI credentials."
        )


class InvalidProfileError(CliUsageError):
    """Raised when a capability profile cannot be loaded."""

    def __init__(self, details: str) -> None:
        super().__init__(
            f"Invalid profile: {details}. Use --profile readonly|developer|eval or a "
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
