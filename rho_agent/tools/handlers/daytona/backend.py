"""Daytona sandbox backend configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DockerfileImage:
    """Build the sandbox image from a local Dockerfile.

    The build context is the Dockerfile's directory.
    """

    path: str


@dataclass
class DaytonaBackend:
    """Daytona sandbox backend configuration (Daytona v2, SDK 2.x).

    ``config`` is a ``daytona.DaytonaConfig`` for auth. When it is None the SDK
    reads ``DAYTONA_API_KEY`` from the environment.

    The sandbox image comes from, in order of precedence:

    - ``snapshot``: the name or ``img_`` id of an image already registered on
      Daytona v2 (Daytona v0 snapshots appear there as images of the same name).
    - ``image``: a container registry reference such as ``python:3.13``, pulled
      into Daytona v2, or a :class:`DockerfileImage` to build.
    """

    config: Any = None  # daytona.DaytonaConfig — lazy import
    image: str | Any = "daytonaio/sandbox:latest"  # registry ref or DockerfileImage
    snapshot: str | None = None  # Registered v2 image (overrides image when set)
    resources: Any = None  # object with cpu / memory / disk (cores, GiB, GiB)
    auto_stop_interval: int = 0  # minutes idle before the sandbox is deleted; 0 = never
    env_vars: dict[str, str] = field(default_factory=dict)
    uv_version: str | None = None
