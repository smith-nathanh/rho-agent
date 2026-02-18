"""Runtime option models."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..capabilities import CapabilityProfile


@dataclass
class RuntimeOptions:
    """Configuration for constructing a runtime."""

    model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL"))
    service_tier: str | None = field(default_factory=lambda: os.getenv("RHO_AGENT_SERVICE_TIER"))
    reasoning_effort: str | None = field(
        default_factory=lambda: os.getenv("RHO_AGENT_REASONING_EFFORT")
    )
    working_dir: str | None = None
    profile: str | CapabilityProfile | None = field(
        default_factory=lambda: os.getenv("RHO_AGENT_PROFILE")
    )
    auto_approve: bool = True
    team_id: str | None = field(default_factory=lambda: os.getenv("RHO_AGENT_TEAM_ID"))
    project_id: str | None = field(default_factory=lambda: os.getenv("RHO_AGENT_PROJECT_ID"))
    observability_config: str | None = field(
        default_factory=lambda: os.getenv("RHO_AGENT_OBSERVABILITY_CONFIG")
    )
    session_id: str | None = None
    telemetry_metadata: dict[str, Any] = field(default_factory=dict)
    response_format: dict[str, Any] | None = None
    enable_delegate: bool = True
