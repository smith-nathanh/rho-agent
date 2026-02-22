"""Agent configuration — portable YAML config for what an agent is and how it runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
    model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5-mini"))
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
    auto_approve: bool = True
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
            "auto_approve",
        }
    )

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
        data["auto_approve"] = self.auto_approve
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
