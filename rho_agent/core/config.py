"""Agent configuration — portable YAML config for what an agent is and how it runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_number(name: str, cast: Any) -> Any:
    """Read an optional number from the environment; unset or empty is None."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return cast(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be a number, got {raw!r}") from err


@dataclass
class AgentConfig:
    """Portable agent configuration — what an agent is and how it runs.

    Captures identity (system prompt + template variables) and infrastructure
    (model, profile, backend, API settings). Designed to live as a YAML file
    in version control so teams can share and version agent definitions.

    Unknown YAML fields are preserved in ``extras`` so teams can stash custom
    metadata (notes, owner, version) in their config files without breaking
    deserialization.

    Usage::

        # From YAML file
        config = AgentConfig.from_file("configs/investigator.yaml")

        # Inline
        config = AgentConfig(
            system_prompt="You are a helpful assistant.",
            profile="developer",
        )

        # Then create an Agent from it
        agent = Agent(config)
    """

    system_prompt: str = ""
    vars: dict[str, str] = field(default_factory=dict)
    model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-6.1-terra"))
    profile: str = field(default_factory=lambda: os.getenv("RHO_AGENT_PROFILE", "readonly"))
    backend: str | Any = field(
        default_factory=lambda: os.getenv("RHO_AGENT_BACKEND", "local")
    )  # str | DaytonaBackend
    working_dir: str | None = None
    base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL"))
    service_tier: str | None = field(default_factory=lambda: os.getenv("RHO_AGENT_SERVICE_TIER"))
    reasoning_effort: str | None = field(
        default_factory=lambda: os.getenv("RHO_AGENT_REASONING_EFFORT")
    )
    response_format: dict[str, Any] | None = None
    databases: dict[str, dict[str, Any]] | None = None
    auto_approve: bool = True
    # Context management. Compaction replaces older history with a summary
    # once the input grows past a threshold.
    #   auto_compact       turn automatic compaction on or off
    #   compact_threshold  fraction of the context window (default 0.7)
    #   compact_at_tokens  exact token count; wins over the fraction
    #   context_window     window size; None looks the model up in core/models.py
    auto_compact: bool = field(default_factory=lambda: _env_bool("RHO_AGENT_AUTO_COMPACT", True))
    compact_threshold: float | None = field(
        default_factory=lambda: _env_number("RHO_AGENT_COMPACT_THRESHOLD", float)
    )
    compact_at_tokens: int | None = field(
        default_factory=lambda: _env_number("RHO_AGENT_COMPACT_AT_TOKENS", int)
    )
    context_window: int | None = None
    # Read AGENTS.md / CLAUDE.md from the working directory and its parents,
    # and list SKILL.md skills, in the system prompt. Local backend only.
    project_instructions: bool = field(
        default_factory=lambda: _env_bool("RHO_AGENT_PROJECT_INSTRUCTIONS", True)
    )
    skills: bool = field(default_factory=lambda: _env_bool("RHO_AGENT_SKILLS", True))
    # Hard caps for one session. None means no cap. The cost cap only works
    # with a client that reports cost (LiteLLMClient).
    max_cost_usd: float | None = None
    max_wall_seconds: float | None = None
    # Longest a single model call may take before it is abandoned and retried.
    max_call_seconds: float | None = None
    # Ask the model to verify its work before a run is allowed to end.
    completion_gate: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    # Known field names (for separating known from extras on YAML load)
    _KNOWN_FIELDS = frozenset(
        {
            "system_prompt",
            "vars",
            "model",
            "profile",
            "backend",
            "working_dir",
            "base_url",
            "service_tier",
            "reasoning_effort",
            "response_format",
            "databases",
            "auto_approve",
            "auto_compact",
            "compact_threshold",
            "compact_at_tokens",
            "context_window",
            "project_instructions",
            "skills",
            "max_cost_usd",
            "max_wall_seconds",
            "max_call_seconds",
            "completion_gate",
        }
    )

    def __post_init__(self) -> None:
        from .models import validate_compaction_settings

        validate_compaction_settings(self.compact_threshold, self.compact_at_tokens)

    @classmethod
    def from_file(cls, path: str | Path) -> AgentConfig:
        """Load config from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a YAML mapping: {path}")
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> AgentConfig:
        """Build an AgentConfig from a plain dict, preserving unknown keys in extras."""
        known: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for key, value in data.items():
            if key in cls._KNOWN_FIELDS:
                known[key] = value
            else:
                extras[key] = value
        known["extras"] = extras
        return cls(**known)

    def to_file(self, path: str | Path) -> None:
        """Save config to a YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._to_dict()
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    def _to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (known fields + extras merged at top level)."""
        data: dict[str, Any] = {}
        if self.system_prompt:
            data["system_prompt"] = self.system_prompt
        if self.vars:
            data["vars"] = dict(self.vars)
        data["model"] = self.model
        data["profile"] = self.profile
        data["backend"] = "daytona" if not isinstance(self.backend, str) else self.backend
        if self.working_dir:
            data["working_dir"] = self.working_dir
        if self.base_url:
            data["base_url"] = self.base_url
        if self.service_tier:
            data["service_tier"] = self.service_tier
        if self.reasoning_effort:
            data["reasoning_effort"] = self.reasoning_effort
        if self.response_format:
            data["response_format"] = self.response_format
        if self.databases:
            data["databases"] = self.databases
        data["auto_approve"] = self.auto_approve
        if not self.auto_compact:
            data["auto_compact"] = False
        if not self.project_instructions:
            data["project_instructions"] = False
        if not self.skills:
            data["skills"] = False
        for key in (
            "compact_threshold",
            "compact_at_tokens",
            "context_window",
            "max_cost_usd",
            "max_wall_seconds",
            "max_call_seconds",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.completion_gate:
            data["completion_gate"] = True
        # Merge extras at top level
        data.update(self.extras)
        return data

    def resolve_system_prompt(self) -> str:
        """Resolve the system prompt to final text.

        Resolution rules:
        - Empty string: look for ~/.config/rho-agent/default.md, then built-in default.md
        - Ends in ``.md``: load as prompt file, render with vars
        - Otherwise: use as inline text
        """
        if not self.system_prompt:
            return self._resolve_default_prompt()
        if self.system_prompt.endswith(".md"):
            return self._resolve_prompt_file(self.system_prompt)
        return self.system_prompt

    def _resolve_default_prompt(self) -> str:
        """Resolve the default system prompt (user default or built-in)."""
        from ..prompts import load_prompt, prepare_prompt

        user_default = Path.home() / ".config" / "rho-agent" / "default.md"
        builtin = Path(__file__).parent.parent / "prompts" / "default.md"
        prompt_path = user_default if user_default.exists() else builtin

        loaded = load_prompt(prompt_path)
        system_prompt, _ = prepare_prompt(loaded, dict(self.vars))
        return system_prompt

    def _resolve_prompt_file(self, path: str) -> str:
        """Load a .md prompt file and render with vars."""
        from ..prompts import load_prompt, prepare_prompt

        loaded = load_prompt(path)
        system_prompt, _ = prepare_prompt(loaded, dict(self.vars))
        return system_prompt
