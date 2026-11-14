"""Bash execution handler, optionally inside the OS sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.truncate import APPROX_BYTES_PER_TOKEN, MAX_OUTPUT_TOKENS
from ..base import ToolHandler, ToolInvocation, ToolOutput
from .paths import sandbox_for

if TYPE_CHECKING:
    from ...sandbox import Sandbox

# Output kept inline before the rest is left in a file. Slightly under the
# session-level truncation limit so the JSON wrapper is never cut.
MAX_INLINE_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS - 500

DEFAULT_TIMEOUT = 300  # seconds


class BashHandler(ToolHandler):
    """Execute shell commands. Standard agentic tool name: 'bash'.

    With a sandbox, every command (and everything it starts) runs inside it,
    with the calling session's access grants applied. The model may ask to run
    one command outside the sandbox (``unsandboxed``); the user must approve
    each such command.

    Each command runs in its own process group with output captured to a file:
    - a timeout or cancel kills the whole group, not only the shell;
    - the call returns when the shell exits, even if it left children running;
    - output too large to return inline stays in the file and its path is given.

    ``run_in_background`` starts a command and returns at once with its PID
    and log path, which the model manages with ordinary commands (``tail``,
    ``kill -0``, ``kill``).
    """

    def __init__(
        self,
        working_dir: str | None = None,
        timeout: int | None = None,
        allow_parallel: bool = False,
        sandbox: Sandbox | None = None,
    ):
        """
        Args:
            working_dir: Default working directory for commands.
            timeout: Default command timeout in seconds (300).
            allow_parallel: If True, several bash calls in one model turn may run concurrently.
            sandbox: Run commands inside this OS sandbox. None runs them with
                the user's own permissions.
        """
        self._working_dir = working_dir or os.getcwd()
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._allow_parallel = allow_parallel
        self._sandbox = sandbox
        self._output_dir: Path | None = None
        self._output_seq = 0

    @property
    def name(self) -> str:
        return "bash"

    @property
    def parallel_safe(self) -> bool:
        return self._allow_parallel

    @property
    def description(self) -> str:
        description = (
            "Execute a bash command. Use for running programs, installing packages, "
            "building code, file operations, and any other shell tasks. "
            "For anything that may run longer than the timeout (training, long builds, "
            "servers), set run_in_background: the call returns at once with a PID and a "
            "log file, which you then check with tail and kill -0."
        )
        if self._sandbox is not None:
            policy = self._sandbox.policy
            description += (
                f" Commands run in a sandbox ({policy.level.value} access): writes outside "
                "the allowed directories fail"
                + ("" if policy.network else " and network access is blocked")
                + ". To get a directory added, use request_access. Set unsandboxed only "
                "for a command that cannot work in the sandbox at all (for example a "
                "system package install); the user must approve it."
            )
        return description

    @property
    def parameters(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "working_dir": {
                "type": "string",
                "description": f"Working directory for the command (default: {self._working_dir})",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default: {self._timeout})",
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Start the command and return immediately with its PID and log file. "
                    "The timeout does not apply. The exit code is appended to the log "
                    "when the command finishes."
                ),
            },
        }
        if self._sandbox is not None:
            properties["unsandboxed"] = {
                "type": "boolean",
                "description": (
                    "Run this one command outside the sandbox. Needs the user's approval; "
                    "give the reason in `reason`. Prefer request_access when a directory "
                    "is all you need."
                ),
            }
            properties["reason"] = {
                "type": "string",
                "description": "With unsandboxed: one sentence, shown to the user.",
            }
        return {
            "type": "object",
            "properties": properties,
            "required": ["command"],
        }

    def _format_output(self, output: str, exit_code: int, duration_seconds: float) -> str:
        """Format output as Codex-style JSON."""
        return json.dumps(
            {
                "output": output,
                "metadata": {
                    "exit_code": exit_code,
                    "duration_seconds": round(duration_seconds, 1),
                },
            },
            indent=2,
        )

    def _new_output_path(self, prefix: str) -> Path:
        if self._output_dir is None:
            self._output_dir = Path(tempfile.mkdtemp(prefix="rho-agent-bash-"))
        self._output_seq += 1
        return self._output_dir / f"{prefix}-{self._output_seq:04d}.log"

    @staticmethod
    def _kill_group(process: asyncio.subprocess.Process) -> None:
        """Kill the command's whole process group (it is its own session leader)."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    @staticmethod
    def _render_output(path: Path, max_bytes: int) -> tuple[str, bool]:
        """Read captured output, returning ``(text, truncated)``.

        Output within ``max_bytes`` is returned whole. Larger output is
        returned as head and tail with a pointer to the file.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return "", False

        if size <= max_bytes:
            return path.read_bytes().decode("utf-8", errors="replace"), False

        half = max_bytes // 2
        with open(path, "rb") as f:
            head = f.read(half).decode("utf-8", errors="replace")
            f.seek(max(size - half, half))
            tail = f.read().decode("utf-8", errors="replace")
        omitted = (size - 2 * half) // APPROX_BYTES_PER_TOKEN
        text = (
            f"{head}\n...{omitted} tokens truncated...\n{tail}\n\n"
            f"[Output was {size} bytes. Full output saved to {path}; "
            f"read parts of it with grep, head, tail or sed -n.]"
        )
        return text, True

    def _read_output(self, path: Path) -> tuple[str, bool]:
        """Read captured output so the formatted result fits the session limit.

        Returns ``(text, kept_on_disk)``. JSON escaping can inflate the text, so
        the inline budget is reduced until the wrapped result fits; otherwise
        the session-level truncation would cut through the JSON wrapper.
        """
        limit = MAX_INLINE_OUTPUT_TOKENS * APPROX_BYTES_PER_TOKEN
        max_bytes = limit
        text, truncated = self._render_output(path, max_bytes)
        for _ in range(4):
            encoded = len(json.dumps(text).encode("utf-8"))
            if encoded <= limit:
                break
            max_bytes = max(int(max_bytes * limit / encoded * 0.9), 512)
            text, truncated = self._render_output(path, max_bytes)
        if not truncated:
            path.unlink(missing_ok=True)
        return text, truncated

    async def _spawn(
        self, command: str, working_dir: str, stdout: Any, sandbox: Sandbox | None
    ) -> asyncio.subprocess.Process:
        """Start ``command`` in its own session, inside ``sandbox`` if there is one.

        stderr goes to the same place as stdout.
        """
        common: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": stdout,
            "stderr": asyncio.subprocess.STDOUT,
            "cwd": working_dir,
            "start_new_session": True,
        }
        if sandbox is None:
            return await asyncio.create_subprocess_shell(command, **common)
        argv = sandbox.wrap(["/bin/sh", "-c", command])
        return await asyncio.create_subprocess_exec(*argv, env=sandbox.env(), **common)

    async def _start_background(
        self, command: str, working_dir: str, start_time: float, sandbox: Sandbox | None
    ) -> ToolOutput:
        """Start a command detached from this call and return its PID and log path."""
        log_path = self._new_output_path("bg")
        # The log is opened here and handed over as stdout, so a sandboxed
        # command needs no write access to its location.
        wrapped = f'( {command}\n) 2>&1; echo "[exit code: $?]"'
        with open(log_path, "wb") as sink:
            process = await self._spawn(wrapped, working_dir, sink, sandbox)
        output = (
            f"Started in background. PID {process.pid} (also its process group).\n"
            f"Log: {log_path}\n"
            f"Check progress: tail -n 40 {log_path}\n"
            f"Still running?: kill -0 {process.pid} && echo running || echo finished\n"
            f"Stop it: kill -- -{process.pid}\n"
            f'The line "[exit code: N]" is appended to the log when it finishes.'
        )
        duration = time.perf_counter() - start_time
        return ToolOutput(
            content=self._format_output(output, exit_code=0, duration_seconds=duration),
            success=True,
            metadata={
                "background": True,
                "pid": process.pid,
                "log_path": str(log_path),
                "command": command,
                "working_dir": working_dir,
            },
        )

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the shell command and return output."""
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._working_dir)
        timeout = invocation.arguments.get("timeout", self._timeout)
        background = bool(invocation.arguments.get("run_in_background", False))

        if not command:
            return ToolOutput(content="No command provided", success=False)

        sandbox = sandbox_for(self._sandbox, invocation)
        if sandbox is not None and invocation.arguments.get("unsandboxed"):
            refusal = await self._approve_unsandboxed(invocation, command, working_dir)
            if refusal:
                return ToolOutput(content=refusal, success=False)
            sandbox = None

        start_time = time.perf_counter()

        try:
            if background:
                return await self._start_background(command, working_dir, start_time, sandbox)

            output_path = self._new_output_path("cmd")
            with open(output_path, "wb") as sink:
                process = await self._spawn(command, working_dir, sink, sandbox)

            timed_out = False
            try:
                # Wait for the shell only. Children it left running keep writing
                # to the file but do not hold this call open.
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.CancelledError:
                self._kill_group(process)
                await process.wait()
                output_path.unlink(missing_ok=True)
                raise
            except TimeoutError:
                timed_out = True
                self._kill_group(process)
                await process.wait()

            duration = time.perf_counter() - start_time
            output, kept = self._read_output(output_path)
            metadata: dict[str, Any] = {
                "duration_seconds": round(duration, 1),
                "command": command,
                "working_dir": working_dir,
            }
            if kept:
                metadata["full_output_path"] = str(output_path)
            metadata["sandbox"] = sandbox.level.value if sandbox is not None else "off"

            if timed_out:
                output += (
                    f"\n\n[Command timed out after {timeout}s; it and its child processes "
                    f"were killed. For long-running commands use run_in_background.]"
                )
                metadata.update(exit_code=-1, timed_out=True)
                return ToolOutput(
                    content=self._format_output(output, exit_code=-1, duration_seconds=duration),
                    success=False,
                    metadata=metadata,
                )

            exit_code = process.returncode
            if exit_code != 0 and sandbox is not None:
                session = invocation.session
                can_request = session is not None and session.approval_callback is not None
                hint = sandbox.denial_hint(output, can_request=can_request)
                if hint:
                    output += f"\n\n{hint}"
            metadata["exit_code"] = exit_code
            return ToolOutput(
                content=self._format_output(output, exit_code, duration),
                success=exit_code == 0,
                metadata=metadata,
            )

        except FileNotFoundError:
            return ToolOutput(
                content=f"Working directory not found: {working_dir}",
                success=False,
            )
        except Exception as e:
            return ToolOutput(
                content=f"Error executing command: {e}",
                success=False,
            )

    async def _approve_unsandboxed(
        self, invocation: ToolInvocation, command: str, working_dir: str
    ) -> str | None:
        """Ask the user to run one command outside the sandbox. None if approved."""
        session = invocation.session
        if session is None or session.approval_callback is None:
            return (
                "Nobody is available to approve an unsandboxed command; work within "
                "the sandbox or tell the user what you need."
            )
        approved = await session.ask_approval(
            "bash",
            {
                "command": command,
                "working_dir": working_dir,
                "unsandboxed": True,
                "reason": str(invocation.arguments.get("reason", "")),
            },
        )
        if not approved:
            return "The user declined running this command outside the sandbox."
        return None
