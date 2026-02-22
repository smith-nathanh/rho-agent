"""Agent — stateless definition of an agent.

Holds the resolved config and tool registry. Reusable across multiple sessions.
Immutable once a Session starts.
"""

from __future__ import annotations

import os
from typing import Any

from ..client.model import ModelClient
from ..tools.registry import ToolRegistry
from .config import AgentConfig


class Agent:
    """Stateless agent definition in the Agent/State/Session decomposition.

    Holds the resolved config (identity + infrastructure) and a tool registry
    (available actions). An Agent is reusable across multiple Sessions — create
    one Agent, run many conversations.

    The registry can be modified (clear, register custom tools) *before* creating
    a Session. Once a Session starts, the registry should be treated as frozen
    because changing tools invalidates the LLM prompt cache.

    Usage::

        agent = Agent(AgentConfig(profile="developer"))
        agent.registry.clear()
        agent.registry.register(MyCustomHandler())
        session = Session(agent)  # registry frozen from here
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self._config = config or AgentConfig()
        self._sandbox_manager: Any | None = None
        self._registry = self._build_registry()
        self._system_prompt: str | None = None  # lazily resolved

    def _build_registry(self) -> ToolRegistry:
        """Build tool registry from the config's profile."""
        from ..permissions.factory import ToolFactory, load_profile

        profile = load_profile(self._config.profile)
        backend = self._config.backend

        # DaytonaBackend instance or "daytona" string
        if not isinstance(backend, str) or backend == "daytona":
            return self._build_daytona_registry(profile)

        factory = ToolFactory(profile)
        return factory.create_registry(working_dir=self._config.working_dir)

    def _build_daytona_registry(self, profile: Any) -> ToolRegistry:
        """Build registry with Daytona remote handlers for shell/file tools."""
        from ..tools.handlers.daytona import DaytonaBackend, register_daytona_tools
        from ..permissions.factory import ToolFactory

        registry = ToolRegistry()
        working_dir = self._config.working_dir or profile.shell_working_dir or "/home/daytona"
        backend = self._config.backend if isinstance(self._config.backend, DaytonaBackend) else None
        self._sandbox_manager = register_daytona_tools(registry, working_dir, backend=backend)

        # Database tools still use the local factory path
        factory = ToolFactory(profile)
        factory._register_database_tools(registry, dict(os.environ))

        return registry

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
