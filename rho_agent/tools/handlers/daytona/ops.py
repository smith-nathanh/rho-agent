"""Helpers over the Daytona v2 SDK (2.x) shared by the tools, CLI and Harbor.

They take the SDK's own sandbox (``daytona.v2.AsyncSandbox``).
"""

from __future__ import annotations

import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Exit codes for a process with no reported exit code, as a shell reports them:
# 124 for a timeout, 128 + n for death by signal n, -1 when unknown.
TIMEOUT_EXIT_CODE = 124
UNKNOWN_EXIT_CODE = -1

# Files per bulk write in upload_paths.
_UPLOAD_BATCH = 50


@dataclass
class CommandResult:
    """Interleaved stdout and stderr of a finished command, and its exit code."""

    output: str
    exit_code: int


def exit_code_of(result: Any) -> int:
    """Exit code of a ``ProcessResult``: 124 when it timed out, 128 + n when killed by signal n."""
    if result.exit_code is not None:
        return int(result.exit_code)
    reason = getattr(result.reason, "value", result.reason)
    if reason == "timed_out":
        return TIMEOUT_EXIT_CODE
    name = getattr(result, "signal", None)
    if name:
        name = str(name).upper()
        try:
            return 128 + signal.Signals[name if name.startswith("SIG") else f"SIG{name}"].value
        except (KeyError, ValueError):
            pass
    return UNKNOWN_EXIT_CODE


def combined_output(result: Any) -> str:
    """Interleaved stdout and stderr of a ``ProcessResult``."""
    if result.output:
        return result.output
    return (result.stdout or "") + (result.stderr or "")


async def run(
    sandbox: Any,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    kill_on_timeout: bool = True,
) -> CommandResult:
    """Run ``command`` through the sandbox shell and wait for it.

    ``timeout`` (seconds) is a server-side deadline: the process tree is killed
    when it passes and the exit code is 124. The client waits 30 s longer. With
    ``kill_on_timeout=False`` the timeout only bounds the wait, so processes the
    command leaves running in the background survive it.
    """
    result = await sandbox.process.run(
        command,
        cwd=cwd,
        env=env or None,
        execution_timeout=float(timeout) if timeout and kill_on_timeout else None,
        timeout=float(timeout) + 30 if timeout else None,
    )
    return CommandResult(output=combined_output(result), exit_code=exit_code_of(result))


async def upload_paths(sandbox: Any, files: list[tuple[Path, str]]) -> None:
    """Write local files to the sandbox, as (local path, remote path) pairs, in bulk."""
    for start in range(0, len(files), _UPLOAD_BATCH):
        batch = {
            remote: local.read_bytes() for local, remote in files[start : start + _UPLOAD_BATCH]
        }
        await sandbox.fs.write_many(batch)


# Daytona v2 adds its own minimal git as /usr/local/bin/git (no `submodule`,
# among others), ahead of the image's git on PATH. When the image has a full git
# of its own, Daytona's is renamed to daytona-git and /usr/local/bin/git becomes
# a link to the image's, so tasks and Daytona's own git service both get it.
# A git is recognised as Daytona's by having no `submodule` command.
DAYTONA_GIT = "/usr/local/bin/git"
_PREFER_IMAGE_GIT = (
    f"[ -x {DAYTONA_GIT} ] && [ ! -L {DAYTONA_GIT} ] || exit 0; "
    f"{DAYTONA_GIT} submodule -h 2>&1 | grep -q 'is not a git command' || exit 0; "
    "for g in /usr/bin/git /bin/git; do "
    f'if [ -x "$g" ] && [ "$(readlink -f "$g")" != "{DAYTONA_GIT}" ]; then '
    f'mv {DAYTONA_GIT} /usr/local/bin/daytona-git && ln -s "$g" {DAYTONA_GIT} '
    "&& echo moved; break; fi; done"
)


async def prefer_image_git(sandbox: Any) -> bool:
    """Replace Daytona's injected git with the image's own, when the image has one.

    Returns True when it was replaced. Runs as root; failures are not raised.
    """
    try:
        result = await sandbox.process.run(_PREFER_IMAGE_GIT, user="root", timeout=60)
    except Exception:
        return False
    return "moved" in (result.stdout or "")
