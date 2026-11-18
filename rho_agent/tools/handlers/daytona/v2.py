"""Daytona v2 sandbox handle with the small interface rho-agent's tools use.

rho-agent's Daytona tools were written against SDK 0.x: ``process.exec``
returning ``.result`` and ``.exit_code``, and ``fs.upload_file`` /
``fs.download_file`` taking bytes or paths. SDK 2.x talks to Daytona v2
with a different interface (``process.run``, ``fs.write_bytes``, ...).
:class:`SandboxV2` wraps a v2 sandbox and keeps the 0.x-shaped calls, so the
tool handlers and the CLI do not change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A v2 process with no reported exit code: timed out, or ended by a signal.
TIMEOUT_EXIT_CODE = 124
UNKNOWN_EXIT_CODE = -1

# Files per bulk write in upload_files.
_UPLOAD_BATCH = 50


@dataclass
class ExecResponse:
    """Result of :meth:`SandboxProcess.exec`: combined output and exit code."""

    result: str
    exit_code: int


@dataclass
class FileUpload:
    """One file for :meth:`SandboxFileSystem.upload_files`.

    ``source`` is a local path or the file's bytes.
    """

    source: str | bytes
    destination: str


def exit_code_of(result: Any) -> int:
    """Exit code of a v2 ``ProcessResult``; 124 when it timed out."""
    if result.exit_code is not None:
        return int(result.exit_code)
    reason = getattr(result.reason, "value", result.reason)
    return TIMEOUT_EXIT_CODE if reason == "timed_out" else UNKNOWN_EXIT_CODE


def combined_output(result: Any) -> str:
    """Interleaved stdout and stderr of a v2 ``ProcessResult``."""
    if result.output:
        return result.output
    return (result.stdout or "") + (result.stderr or "")


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


async def prefer_image_git(raw_sandbox: Any) -> bool:
    """Move Daytona's injected git aside when the image has its own.

    Returns True when it was moved. Runs as root; failures are not raised.
    """
    try:
        result = await raw_sandbox.process.run(_PREFER_IMAGE_GIT, user="root", timeout=60)
    except Exception:
        return False
    return "moved" in (result.stdout or "")


class SandboxProcess:
    def __init__(self, sandbox: Any):
        self._sandbox = sandbox

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResponse:
        """Run ``command`` through the sandbox shell and wait for it.

        ``timeout`` (seconds) is a server-side deadline: the process is killed
        when it passes, and the exit code is 124.
        """
        result = await self._sandbox.process.run(
            command,
            cwd=cwd,
            env=env or None,
            execution_timeout=float(timeout) if timeout else None,
            # The client waits a little longer than the server-side deadline.
            timeout=float(timeout) + 30 if timeout else None,
        )
        return ExecResponse(result=combined_output(result), exit_code=exit_code_of(result))


class SandboxFileSystem:
    def __init__(self, sandbox: Any):
        self._sandbox = sandbox

    async def upload_file(self, source: str | os.PathLike[str] | bytes, destination: str) -> None:
        """Write ``source`` (bytes, or a local file path) to ``destination``."""
        if isinstance(source, (bytes, bytearray, memoryview)):
            await self._sandbox.fs.write_bytes(destination, bytes(source))
        else:
            await self._sandbox.fs.upload_file(str(source), destination)

    async def upload_files(self, files: list[FileUpload]) -> None:
        """Write several files, in batches of bulk writes."""
        for start in range(0, len(files), _UPLOAD_BATCH):
            batch: dict[str, bytes] = {}
            for upload in files[start : start + _UPLOAD_BATCH]:
                data = upload.source
                if not isinstance(data, (bytes, bytearray, memoryview)):
                    data = Path(data).read_bytes()
                batch[upload.destination] = bytes(data)
            await self._sandbox.fs.write_many(batch)

    async def download_file(
        self, source: str, destination: str | os.PathLike[str] | None = None
    ) -> bytes | None:
        """Return the file's bytes, or save it to ``destination`` when given."""
        if destination is None:
            return await self._sandbox.fs.read_bytes(source)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        await self._sandbox.fs.download_file(source, str(destination))
        return None


class SandboxV2:
    """A Daytona v2 sandbox with the 0.x-shaped ``process`` and ``fs`` calls.

    ``raw`` is the SDK's own sandbox handle, for anything else.
    """

    def __init__(self, raw: Any):
        self.raw = raw
        self.process = SandboxProcess(raw)
        self.fs = SandboxFileSystem(raw)

    @property
    def id(self) -> str:
        return self.raw.id

    async def delete(self) -> None:
        await self.raw.delete()
