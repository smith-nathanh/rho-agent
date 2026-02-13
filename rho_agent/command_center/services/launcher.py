"""Subprocess-based agent launcher/tracker.

This service is additive to the existing signal-based control path: launched agents
still use the same session_id mechanism and signal files for pause/resume/kill.

The launcher maintains an in-memory registry of subprocess handles so that a TUI
can show and manage agents started from this process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from rho_agent.command_center.models import LaunchedAgent, LaunchRequest, ManagedProcess


@dataclass(slots=True)
class _RegistryEntry:
    managed: ManagedProcess
    popen: subprocess.Popen[str]


class AgentLauncher:
    """Launch and manage agent subprocesses started from this process."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        entrypoint: list[str] | None = None,
    ) -> None:
        self._popen_factory = popen_factory
        # Prefer running via current interpreter and CLI module path.
        self._entrypoint = entrypoint or [sys.executable, "-m", "rho_agent.cli"]
        self._registry: dict[str, _RegistryEntry] = {}

    def _build_command(self, request: LaunchRequest, *, session_id: str) -> list[str]:
        cmd: list[str] = [
            *self._entrypoint,
            "main",
            "--profile",
            request.profile,
            "--model",
            request.model,
            "--session-id",
            session_id,
            "--working-dir",
            os.fspath(request.working_dir),
        ]
        if request.team_id:
            cmd.extend(["--team-id", request.team_id])
        if request.project_id:
            cmd.extend(["--project-id", request.project_id])
        if request.auto_approve:
            cmd.append("--auto-approve")
        if request.prompt:
            cmd.append(request.prompt)
        return cmd

    def launch(self, request: LaunchRequest) -> LaunchedAgent:
        session_id = uuid.uuid4().hex[:8]
        command = self._build_command(request, session_id=session_id)

        proc = self._popen_factory(
            command,
            cwd=os.fspath(request.working_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

        managed = ManagedProcess(
            session_id=session_id,
            pid=int(proc.pid),
            command=command,
            working_dir=Path(request.working_dir),
        )
        self._registry[session_id] = _RegistryEntry(managed=managed, popen=proc)

        return LaunchedAgent(
            session_id=session_id,
            pid=int(proc.pid),
            command=command,
            started_at=datetime.now(),
        )

    def list_managed(self) -> list[ManagedProcess]:
        """List managed processes, cleaning up any that have exited."""
        exited: list[str] = []
        for session_id, entry in self._registry.items():
            if entry.popen.poll() is not None:
                exited.append(session_id)
        for session_id in exited:
            self._registry.pop(session_id, None)
        return [entry.managed for entry in self._registry.values()]

    def stop_managed(self, session_id: str, *, timeout_s: float = 2.0) -> bool:
        """Stop a managed process.

        Returns True if a managed process was found (and a stop attempt was made),
        else False.
        """

        entry = self._registry.get(session_id)
        if entry is None:
            return False

        proc = entry.popen
        if proc.poll() is not None:
            self._registry.pop(session_id, None)
            return True

        try:
            # start_new_session=True creates a new process group; terminate the group.
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=timeout_s)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=timeout_s)
            except Exception:
                pass

        if proc.poll() is not None:
            self._registry.pop(session_id, None)
        return True
