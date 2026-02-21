"""Agent — stateless definition of an agent.

Holds the resolved config and tool registry. Reusable across multiple sessions.
Immutable once a Session starts.
"""

from __future__ import annotations

from ..client.model import ModelClient
from ..tools.registry import ToolRegistry
from .config import AgentConfig

# Re-export shared types for backwards compatibility
from .events import (  # noqa: F401
    AUTO_COMPACT_THRESHOLD,
    COMPACTION_SYSTEM_PROMPT,
    COMPLETION_SIGNALS,
    MAX_NUDGES,
    NUDGE_MESSAGE,
    SUMMARY_PREFIX,
    AgentEvent,
    ApprovalCallback,
    ApprovalInterrupt,
    CompactCallback,
    CompactResult,
    EventHandler,
    RunResult,
)


class Agent:
    """Stateless agent definition — config + tool registry.

    Create one agent, run many conversations via Session.
    Modify registry before creating a Session; it's frozen after that.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self._config = config or AgentConfig()
        self._registry = self._build_registry()
        self._system_prompt: str | None = None  # lazily resolved

    def _build_registry(self) -> ToolRegistry:
        """Build tool registry from the config's profile."""
        from ..capabilities.factory import ToolFactory, load_profile

        profile = load_profile(self._config.profile)
        factory = ToolFactory(profile)
        return factory.create_registry(working_dir=self._config.working_dir)

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def system_prompt(self) -> str:
        """Resolved system prompt text (lazily computed)."""
        if self._system_prompt is None:
            self._system_prompt = self._config.resolve_system_prompt()
        return self._system_prompt

    def create_client(self) -> ModelClient:
        """Create a ModelClient from this agent's config."""
        return ModelClient(
            model=self._config.model,
            base_url=self._config.base_url,
            service_tier=self._config.service_tier,
            reasoning_effort=self._config.reasoning_effort,
            response_format=self._config.response_format,
        )
