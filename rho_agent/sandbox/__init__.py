"""Access control: the OS sandbox around commands the agent runs.

Commands spawned by tools (bash, grep, glob) run under the operating system's
sandbox, so the kernel refuses writes outside the allowed directories and,
optionally, network access. Tools that touch files in-process (read, write,
edit, ...) check the same policy themselves.

- macOS: Seatbelt (``sandbox-exec``), built in.
- Linux: bubblewrap (``bwrap``), from the distribution's ``bubblewrap`` package.
- Windows: not supported; use WSL2, or ``full`` access in a container.

The agent process itself is not sandboxed: it needs the network for the model
API and its own session files.
"""

from __future__ import annotations

import functools
import os
import platform
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from . import bubblewrap, seatbelt, seccomp
from .policy import (
    DEFAULT_DENY_READ,
    DEFAULT_DENY_WRITE,
    DEFAULT_ENV_SCRUB,
    DEFAULT_WRITABLE_CACHES,
    Access,
    AccessLevel,
    Grant,
    ResolvedPolicy,
)


class SandboxUnavailableError(RuntimeError):
    """The access level needs a sandbox that cannot run on this machine."""


# Output that sandbox refusals produce, used to point the model at the cause.
DENIAL_MARKERS = ("Operation not permitted", "Read-only file system")
# bubblewrap hides unreadable paths, so reading one fails this way instead.
BWRAP_DENIAL_MARKERS = (*DENIAL_MARKERS, "Permission denied")


class Sandbox:
    """A resolved policy and the OS mechanism that enforces it.

    Shared by a registry's tools. Grants made in a session are applied per
    call with ``with_grants``, so sessions sharing an agent do not share them.
    """

    def __init__(self, policy: ResolvedPolicy, backend: str) -> None:
        self.policy = policy
        self.backend = backend  # "seatbelt" | "bubblewrap"
        self._granted: dict[tuple[Grant, ...], Sandbox] = {}

    @property
    def level(self) -> AccessLevel:
        return self.policy.level

    def with_grants(self, grants: Iterable[Grant]) -> Sandbox:
        """This sandbox with session grants applied (cached per set of grants)."""
        key = tuple(grants)
        if not key:
            return self
        if key not in self._granted:
            self._granted[key] = Sandbox(self.policy.with_grants(key), self.backend)
        return self._granted[key]

    def wrap(self, argv: list[str]) -> list[str]:
        """Prefix a command's argv so it runs inside the sandbox."""
        if self.backend == "seatbelt":
            return seatbelt.wrap(self.policy, argv)
        return bubblewrap.wrap(self.policy, argv)

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for a sandboxed command (secrets removed)."""
        return self.policy.child_env(base)

    def check_write(self, path: str | Path) -> tuple[bool, str]:
        return self.policy.can_write(path)

    def check_read(self, path: str | Path) -> tuple[bool, str]:
        return self.policy.can_read(path)

    def describe(self) -> str:
        name = {"seatbelt": "macOS Seatbelt", "bubblewrap": "bubblewrap"}.get(
            self.backend, self.backend
        )
        return f"{self.policy.describe()}\n- Mechanism: {name}."

    def denial_hint(self, output: str, *, can_request: bool) -> str | None:
        """A note for the model when command output looks like a sandbox refusal."""
        markers = BWRAP_DENIAL_MARKERS if self.backend == "bubblewrap" else DENIAL_MARKERS
        lowered = output.lower()
        if not any(marker.lower() in lowered for marker in markers):
            return None
        network = "allowed" if self.policy.network else "blocked"
        if not self.policy.unix_sockets:
            network += "; Unix sockets (Docker, ssh-agent, ...) blocked"
        next_step = (
            "If you need a directory outside these, call request_access with the "
            "directory and a reason. If a command needs a blocked local service "
            "(such as Docker), run it with unsandboxed: true and a reason."
            if can_request
            else "Work inside the writable directories, or tell the user what you need."
        )
        return (
            f"[Sandbox ({self.level.value}): this may have been blocked by the sandbox. "
            f"Writable: {', '.join(self.policy.writable_summary())}. Network: {network}. "
            f"{next_step}]"
        )


def backend_name() -> str | None:
    """The sandbox backend for this platform, or None if there is none."""
    if sys.platform == "darwin":
        return "seatbelt"
    if sys.platform.startswith("linux"):
        return "bubblewrap"
    return None


@functools.cache
def _probe(backend: str) -> str | None:
    """Try the backend once. Returns None if it works, else why not."""
    if backend == "seatbelt":
        if not seatbelt.available():
            return f"{seatbelt.SANDBOX_EXEC} not found"
        argv = seatbelt.probe_argv()
    else:
        if not bubblewrap.available():
            return (
                "bubblewrap is not installed. Install it with your package manager "
                "(e.g. `sudo apt install bubblewrap` or `sudo dnf install bubblewrap`)"
            )
        argv = bubblewrap.probe_argv()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{backend} failed to start: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit code {result.returncode}"
        hint = ""
        if backend == "bubblewrap":
            hint = (
                " On Ubuntu 24.04+, unprivileged user namespaces may be restricted by "
                "AppArmor (kernel.apparmor_restrict_unprivileged_userns). Inside a "
                "container, use full access; the container is the boundary."
            )
        elif "Operation not permitted" in reason:
            hint = " rho-agent may already be running inside a sandbox."
        return f"{backend} cannot create a sandbox here: {reason}.{hint}"
    return None


def check_backend() -> tuple[str | None, str | None]:
    """Return ``(backend, error)``: the usable backend, or why there is none."""
    backend = backend_name()
    if backend is None:
        return None, f"there is no sandbox for platform {sys.platform!r} (use WSL2)"
    error = _probe(backend)
    return (None, error) if error else (backend, None)


def create_sandbox(access: Access | str, working_dir: str | Path | None) -> Sandbox | None:
    """Build the sandbox an access level needs, or None for full access.

    Raises:
        SandboxUnavailableError: the level needs a sandbox this machine cannot
            provide. rho-agent never falls back to running unsandboxed.
    """
    access = Access.from_value(access)
    if not access.sandboxed:
        return None
    backend, error = check_backend()
    if backend is None:
        raise SandboxUnavailableError(
            f"No sandbox available: {error}. Fix that, or use --access full "
            '(access="full") if this machine is disposable.'
        )
    if backend == "bubblewrap" and not access.unix_sockets and not seccomp.supported():
        raise SandboxUnavailableError(
            "No sandbox available: rho-agent cannot block Unix sockets on this CPU "
            f"architecture ({platform.machine()}). Set unix_sockets: true in the access "
            "settings to run without that protection, or use --access full."
        )
    return Sandbox(access.resolve(working_dir or os.getcwd()), backend)


__all__ = [
    "DEFAULT_DENY_READ",
    "DEFAULT_DENY_WRITE",
    "DEFAULT_ENV_SCRUB",
    "DEFAULT_WRITABLE_CACHES",
    "DENIAL_MARKERS",
    "Access",
    "AccessLevel",
    "Grant",
    "ResolvedPolicy",
    "Sandbox",
    "SandboxUnavailableError",
    "backend_name",
    "check_backend",
    "create_sandbox",
]
