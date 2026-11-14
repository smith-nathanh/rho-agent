"""Linux backend: bubblewrap (bwrap) mount namespaces.

The whole filesystem is bind-mounted read-only, then the writable roots are
bind-mounted read-write on top, then protected paths are made read-only (or,
for unreadable paths, hidden) again. The network namespace is unshared when
the policy turns the network off. Unless the policy allows Unix sockets, a
seccomp filter stops commands creating them (see ``seccomp``). A new PID namespace means a killed command
takes all its children with it.

Bind mounts need their source to exist, so paths that do not exist are
skipped: a missing writable root is simply not writable, and a missing
protected path has nothing to protect (except that a command could create it;
git creates ``.git/hooks`` on init, so this rarely matters).
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess

from . import seccomp
from .policy import ResolvedPolicy


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


@functools.cache
def _supports_perms() -> bool:
    """``--perms`` (mode for the next mount) arrived in bubblewrap 0.5.0."""
    try:
        out = subprocess.run(
            [bwrap_path() or "bwrap", "--version"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.search(r"(\d+)\.(\d+)", out)
    return bool(match) and (int(match[1]), int(match[2])) >= (0, 5)


def build_args(policy: ResolvedPolicy) -> list[str]:
    """bwrap options (without the command) for the policy."""
    args = [
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    for path in policy.writable:
        if os.path.exists(path):
            args += ["--bind", path, path]
    for path in policy.deny_write:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    for path in policy.deny_read:
        if os.path.isdir(path):
            # An empty, read-only tmpfs over the directory. Mode 000 makes
            # listing it fail with "Permission denied", so the agent sees a
            # refusal rather than an empty directory (older bwrap: just empty).
            perms = ["--perms", "0000"] if _supports_perms() else []
            args += [*perms, "--tmpfs", path, "--remount-ro", path]
        elif os.path.exists(path):
            args += ["--ro-bind", "/dev/null", path]
    if not policy.network:
        args.append("--unshare-net")
    # No --chdir: bwrap keeps the caller's current directory, which is the
    # command's working directory.
    return args


def wrap(policy: ResolvedPolicy, argv: list[str]) -> list[str]:
    """Prefix ``argv`` so it runs under the policy."""
    bwrap = bwrap_path() or "bwrap"
    if policy.unix_sockets:
        return [bwrap, *build_args(policy), "--", *argv]
    prefix, options = seccomp.load_args(seccomp.filter_path())
    return [*prefix, bwrap, *build_args(policy), *options, "--", *argv]


def available() -> bool:
    return bwrap_path() is not None


def probe_argv() -> list[str]:
    """A command that succeeds only if bwrap can create namespaces here (and
    load the Unix-socket filter, where there is one for this machine)."""
    prefix, options = ([], [])
    if seccomp.supported():
        prefix, options = seccomp.load_args(seccomp.filter_path())
    return [
        *prefix,
        bwrap_path() or "bwrap",
        *options,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--unshare-pid",
        "--unshare-net",
        "--",
        "true",
    ]
