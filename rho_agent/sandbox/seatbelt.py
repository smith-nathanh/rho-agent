"""macOS backend: Seatbelt profiles run with /usr/bin/sandbox-exec.

The profile allows everything by default, then denies:
- file writes outside the writable roots (and a few device files);
- file writes to protected paths inside them;
- file reads of protected paths;
- IP networking, when the policy turns the network off;
- connections to Unix sockets (Docker, ssh-agent, tmux, ...), unless the
  policy allows them, except the system's DNS and logging sockets.

Known gap: because the base is ``allow default``, Mach services stay reachable,
so a command can still ask other, unsandboxed processes to act for it (for
example ``open`` or ``osascript`` driving another app). Closing that needs a
deny-by-default profile with an allowlist of system services (DNS, trustd,
keychain, ...), as Codex and Anthropic's sandbox-runtime use; tested
denies of individual services did not block it.

Paths are passed as -D parameters so they need no quoting in the profile.
"""

from __future__ import annotations

import os

from .policy import ResolvedPolicy

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Unix sockets commands may always connect to: DNS lookups go through
# mDNSResponder, and syslog() through the syslog socket.
_SYSTEM_SOCKETS = ("/private/var/run/mDNSResponder", "/private/var/run/syslog")

# Device files any program may write (terminal output, /dev/null, ...).
_DEVICE_WRITES = (
    '(literal "/dev/null")',
    '(literal "/dev/zero")',
    '(literal "/dev/tty")',
    '(literal "/dev/ptmx")',
    '(literal "/dev/dtracehelper")',
    '(regex #"^/dev/fd/")',
    '(regex #"^/dev/ttys[0-9]+$")',
)


def build_profile(policy: ResolvedPolicy) -> tuple[str, list[str]]:
    """Return the Seatbelt profile text and its ``-D NAME=VALUE`` arguments."""
    params: list[str] = []

    def param(prefix: str, index: int, value: str) -> str:
        name = f"{prefix}{index}"
        params.extend(["-D", f"{name}={value}"])
        return f'(param "{name}")'

    rules = ["(version 1)", "(allow default)"]

    allowed = [f"(require-not (subpath {param('W', i, p)}))" for i, p in enumerate(policy.writable)]
    allowed += [f"(require-not {dev})" for dev in _DEVICE_WRITES]
    rules.append("(deny file-write*\n  (require-all\n    " + "\n    ".join(allowed) + "))")

    if policy.deny_write:
        paths = " ".join(f"(subpath {param('DW', i, p)})" for i, p in enumerate(policy.deny_write))
        rules.append(f"(deny file-write* {paths})")

    if policy.deny_read:
        paths = " ".join(f"(subpath {param('DR', i, p)})" for i, p in enumerate(policy.deny_read))
        rules.append(f"(deny file-read* {paths})")

    if not policy.network:
        rules.append("(deny network-outbound (remote ip))")
        rules.append("(deny network-inbound (local ip))")
        rules.append("(deny network-bind (local ip))")

    if not policy.unix_sockets:
        rules.append("(deny network-outbound (remote unix-socket))")
        allowed_sockets = " ".join(
            f'(remote unix-socket (path-literal "{path}"))' for path in _SYSTEM_SOCKETS
        )
        rules.append(f"(allow network-outbound {allowed_sockets})")

    return "\n".join(rules), params


def wrap(policy: ResolvedPolicy, argv: list[str]) -> list[str]:
    """Prefix ``argv`` so it runs under the policy."""
    profile, params = build_profile(policy)
    return [SANDBOX_EXEC, "-p", profile, *params, "--", *argv]


def available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


def probe_argv() -> list[str]:
    """A command that succeeds only if sandbox-exec can apply a profile here."""
    return [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "--", "/usr/bin/true"]
