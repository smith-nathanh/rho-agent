"""Daytona sandbox backend configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DaytonaBackend:
    """Daytona sandbox backend configuration.

    Wraps the SDK's DaytonaConfig for auth and adds sandbox shape params.
    When config is None, the SDK reads auth from env vars / .env files.
    """

    config: Any = None  # daytona.DaytonaConfig — lazy import
    image: str | Any = "daytonaio/sandbox:latest"  # str or daytona.Image
    snapshot: str | None = None  # Named snapshot (overrides image when set)
    resources: Any = None  # daytona.Resources — lazy import
    auto_stop_interval: int = 0
    env_vars: dict[str, str] = field(default_factory=dict)
    uv_version: str | None = None
