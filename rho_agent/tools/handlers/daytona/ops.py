"""Helpers over the Daytona v2 SDK (2.x) shared by the tools, CLI and Harbor.

They take the SDK's own sandbox (``daytona.v2.AsyncSandbox``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A process with no reported exit code: timed out, or ended by a signal.
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
    """Exit code of a ``ProcessResult``; 124 when it timed out."""
    if result.exit_code is not None:
        return int(result.exit_code)
    reason = getattr(result.reason, "value", result.reason)
    return TIMEOUT_EXIT_CODE if reason == "timed_out" else UNKNOWN_EXIT_CODE


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
) -> CommandResult:
    """Run ``command`` through the sandbox shell and wait for it.

    ``timeout`` (seconds) is a server-side deadline: the process is killed when
    it passes and the exit code is 124. The client waits 30 s longer.
    """
    result = await sandbox.process.run(
        command,
        cwd=cwd,
        env=env or None,
        execution_timeout=float(timeout) if timeout else None,
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


# Daytona v2 adds its own minimal git (no `submodule`, among others) as
# /usr/local/bin/git, ahead of the image's git on PATH. When the image has a
# git of its own, move Daytona's aside so the image's is used.
DAYTONA_GIT = "/usr/local/bin/git"
_PREFER_IMAGE_GIT = (
    "for g in /usr/bin/git /bin/git; do "
    f'if [ -x "$g" ] && [ -e {DAYTONA_GIT} ] && '
    f'[ "$(readlink -f "$g")" != "$(readlink -f {DAYTONA_GIT})" ]; then '
    f"mv {DAYTONA_GIT} /usr/local/bin/daytona-git && echo moved; break; fi; done"
)


async def prefer_image_git(sandbox: Any) -> bool:
    """Move Daytona's injected git aside when the image has its own.

    Returns True when it was moved. Runs as root; failures are not raised.
    """
    try:
        result = await sandbox.process.run(_PREFER_IMAGE_GIT, user="root", timeout=60)
    except Exception:
        return False
    return "moved" in (result.stdout or "")
