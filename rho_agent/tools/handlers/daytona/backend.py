"""Daytona sandbox backend configuration (Daytona v2, SDK 2.x)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DockerfileImage:
    """Build the sandbox image from a local Dockerfile on Daytona v2.

    The build context is the Dockerfile's directory.
    """

    path: str


@dataclass(frozen=True)
class RegisteredImage:
    """Boot an image already registered in the Daytona v2 project, by name or ``img_`` id."""

    name: str


@dataclass
class DaytonaBackend:
    """Daytona sandbox backend configuration.

    ``config`` is a ``daytona.DaytonaConfig`` for auth; when it is None the SDK
    reads ``DAYTONA_API_KEY`` from the environment.

    ``image`` is one of: a container registry reference such as
    ``"python:3.13"`` (pulled into Daytona v2), a :class:`DockerfileImage` to
    build, or a :class:`RegisteredImage` already on Daytona v2.

    ``resources`` is a ``daytona.v2.Resources`` (cores, memory GiB, disk GiB).
    """

    config: Any = None  # daytona.DaytonaConfig — lazy import
    image: str | DockerfileImage | RegisteredImage = "daytonaio/sandbox:latest"
    resources: Any = None  # daytona.v2.Resources — lazy import
    idle_timeout_minutes: int = 0  # delete the sandbox after this long idle; 0 = never
    env_vars: dict[str, str] = field(default_factory=dict)
    uv_version: str | None = None
