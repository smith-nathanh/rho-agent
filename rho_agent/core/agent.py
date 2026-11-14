"""Agent — stateless definition of an agent.

Holds the resolved config and tool registry. Reusable across multiple sessions.
Immutable once a Session starts.
"""

from __future__ import annotations

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

        agent = Agent(AgentConfig(access="read-only"))
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
        """Build the tool registry from the config's access level and tools."""
        backend = self._config.backend

        # DaytonaBackend instance or "daytona" string
        if not isinstance(backend, str) or backend == "daytona":
            return self._build_daytona_registry()

        from ..tools.factory import build_registry

        config = self._config
        return build_registry(
            config.access,
            working_dir=config.working_dir,
            tools=config.tools,
            databases=config.databases,
            parallel_bash=config.parallel_bash,
        )

    def _build_daytona_registry(self) -> ToolRegistry:
        """Remote handlers for shell and file tools; the remote VM is the boundary."""
        from ..sandbox import Access
        from ..tools.factory import build_registry
        from ..tools.handlers.daytona import DaytonaBackend, register_daytona_tools

        config = self._config
        registry = ToolRegistry()
        working_dir = config.working_dir or "/home/daytona"
        backend = config.backend if isinstance(config.backend, DaytonaBackend) else None
        wanted = None if config.tools is None else set(config.tools)
        self._sandbox_manager = register_daytona_tools(
            registry,
            working_dir,
            backend=backend,
            view_image=wanted is None or "view_image" in wanted,
        )
        if wanted is not None:
            for name in list(registry._handlers):
                if name not in wanted:
                    registry.unregister(name)

        # Database and delegate tools run locally. Full access: no local sandbox,
        # and only these tools are taken from the local registry.
        local_names = {"delegate", "sqlite", "postgres", "mysql", "oracle", "vertica"}
        local = build_registry(
            Access(level="full") if config.access.sandboxed else config.access,
            working_dir=None,
            tools=sorted(local_names & wanted) if wanted is not None else sorted(local_names),
            databases=config.databases,
        )
        for handler in local._handlers.values():
            registry.register(handler)
        return registry

    @property
    def sandbox_manager(self) -> Any | None:
        return self._sandbox_manager

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
            context = self._project_context()
            if context:
                self._system_prompt = f"{self._system_prompt}\n\n{context}"
        return self._system_prompt

    def _project_context(self) -> str:
        """AGENTS.md instructions and the skill list, for the local backend."""
        config = self._config
        if config.backend != "local":
            return ""  # the files would be read from this machine, not the sandbox
        if not (config.project_instructions or config.skills):
            return ""
        from .project_context import build_context

        return build_context(
            config.working_dir,
            instructions=config.project_instructions,
            skills=config.skills,
        )

    def create_client(self) -> ModelClient:
        """Create a ModelClient from this agent's config."""
        return ModelClient(
            model=self._config.model,
            base_url=self._config.base_url,
            service_tier=self._config.service_tier,
            reasoning_effort=self._config.reasoning_effort,
            response_format=self._config.response_format,
        )
