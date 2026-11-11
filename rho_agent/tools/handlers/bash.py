"""Bash execution handler with configurable restrictions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from ...core.truncate import APPROX_BYTES_PER_TOKEN, MAX_OUTPUT_TOKENS
from ..base import ToolHandler, ToolInvocation, ToolOutput

# Output kept inline before the rest is left in a file. Slightly under the
# session-level truncation limit so the JSON wrapper is never cut.
MAX_INLINE_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS - 500

DEFAULT_TIMEOUT_RESTRICTED = 120  # seconds
DEFAULT_TIMEOUT_UNRESTRICTED = 300  # 5 minutes for complex builds

# Allowlist of safe read-only commands (used in RESTRICTED mode)
ALLOWED_COMMANDS = {
    # File inspection
    "cat",
    "head",
    "tail",
    "less",
    "more",
    # Search
    "grep",
    "rg",
    "ag",
    "ack",
    "find",
    "locate",
    "which",
    "whereis",
    # Directory listing
    "ls",
    "tree",
    "du",
    "df",
    # File info
    "file",
    "stat",
    "wc",
    "md5",
    "sha256sum",
    "shasum",
    # Text processing (read-only)
    "awk",
    "sed",  # Note: sed -i is blocked by pipe check
    "cut",
    "sort",
    "uniq",
    "tr",
    "column",
    "fmt",
    "fold",
    "nl",
    "pr",
    "expand",
    "unexpand",
    # JSON/YAML/XML
    "jq",
    "yq",
    "xmllint",
    # Archive inspection (read-only)
    "tar",  # listing only, extraction blocked by write check
    "unzip",  # -l listing only
    "zipinfo",
    "zcat",
    "zless",
    "zgrep",
    "gzip",  # -l listing
    "gunzip",  # to stdout only
    # System info
    "pwd",
    "whoami",
    "hostname",
    "uname",
    "env",
    "printenv",
    "date",
    "uptime",
    "ps",
    "top",
    "free",
    # Networking (read-only)
    "ping",
    "curl",
    "wget",
    "dig",
    "nslookup",
    "host",
    "netstat",
    "ss",
    # Git (read-only)
    "git",
    # Misc
    "echo",
    "printf",
    "diff",
    "cmp",
    "comm",
    "hexdump",
    "xxd",
    "od",
    "strings",
}

# Patterns that indicate write operations (blocked in RESTRICTED mode)
DANGEROUS_SUBSTRINGS = [
    ">>",  # Redirect (append)
    ">",  # Redirect (overwrite)
]

DANGEROUS_COMMAND_WORDS = {
    "rm",
    "rmdir",
    "mv",
    "cp",
    "chmod",
    "chown",
    "chgrp",
    "mkdir",
    "touch",
    "truncate",
    "shred",
    "dd",
    "mkfs",
    "mount",
    "umount",
    "kill",
    "pkill",
    "killall",
    "reboot",
    "shutdown",
    "halt",
    "poweroff",
    "systemctl",
    "service",
    "apt",
    "yum",
    "dnf",
    "brew",
    "pip",
    "npm",
    "yarn",
    "cargo",
    "sudo",
    "su",
    "doas",
}


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc body text so safety checks only inspect executed shell syntax."""
    lines = command.splitlines()
    if not lines:
        return command

    result: list[str] = []
    active_delimiter: str | None = None

    for line in lines:
        if active_delimiter is not None:
            if line.strip() == active_delimiter:
                active_delimiter = None
            continue

        result.append(line)
        match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if match:
            active_delimiter = match.group(2)

    return "\n".join(result)


def _contains_dangerous_word(command: str) -> str | None:
    """Return first dangerous command word matched as a standalone token."""
    lowered = command.lower()
    for word in sorted(DANGEROUS_COMMAND_WORDS, key=len, reverse=True):
        pattern = rf"(^|[\s;&|()]){re.escape(word)}(?=$|[\s;&|()])"
        if re.search(pattern, lowered):
            return word
    return None


def extract_base_command(command: str) -> str | None:
    """Extract the base command from a shell command string."""
    # Handle pipes - check first command
    if "|" in command:
        command = command.split("|")[0].strip()

    # Handle command chaining - check first command
    for sep in ["&&", ";", "||"]:
        if sep in command:
            command = command.split(sep)[0].strip()

    # Handle env vars at start (VAR=value cmd)
    parts = command.split()
    for _i, part in enumerate(parts):
        if "=" not in part:
            return part

    return parts[0] if parts else None


def is_command_allowed(command: str) -> tuple[bool, str]:
    """Check if a command is allowed in RESTRICTED mode. Returns (allowed, reason)."""
    command_for_checks = _strip_heredoc_bodies(command)

    # Check for dangerous patterns first
    for pattern in DANGEROUS_SUBSTRINGS:
        if pattern in command_for_checks:
            return False, f"Command contains dangerous pattern: {pattern}"

    dangerous_word = _contains_dangerous_word(command_for_checks)
    if dangerous_word:
        return False, f"Command contains dangerous pattern: {dangerous_word}"

    # Extract base command
    base_cmd = extract_base_command(command_for_checks)
    if not base_cmd:
        return False, "Could not parse command"

    # Check allowlist
    if base_cmd not in ALLOWED_COMMANDS:
        return False, f"Command '{base_cmd}' is not in the allowlist"

    return True, ""


class BashHandler(ToolHandler):
    """Execute shell commands with configurable restrictions.

    Standard agentic tool name: 'bash'

    Modes:
    - RESTRICTED: Only allowlisted commands, dangerous patterns blocked
    - UNRESTRICTED: Any command allowed (container provides sandbox)

    Each command runs in its own process group with output captured to a file:
    - a timeout or cancel kills the whole group, not only the shell;
    - the call returns when the shell exits, even if it left children running;
    - output too large to return inline stays in the file and its path is given.

    In unrestricted mode ``run_in_background`` starts a command and returns at
    once with its PID and log path, which the model manages with ordinary
    commands (``tail``, ``kill -0``, ``kill``).
    """

    def __init__(
        self,
        restricted: bool = True,
        working_dir: str | None = None,
        timeout: int | None = None,
        requires_approval: bool | None = None,
        allow_parallel: bool = False,
    ):
        """Initialize BashHandler.

        Args:
            restricted: If True, use command allowlist (default). If False, allow all commands.
            working_dir: Default working directory for commands.
            timeout: Command timeout in seconds. Defaults to 120s (restricted) or 300s (unrestricted).
            requires_approval: Override whether approval is required. Defaults to True for restricted,
                              False for unrestricted.
            allow_parallel: If True, several bash calls in one model turn may run concurrently.
        """
        self._restricted = restricted
        self._working_dir = working_dir or os.getcwd()
        self._timeout = timeout or (
            DEFAULT_TIMEOUT_RESTRICTED if restricted else DEFAULT_TIMEOUT_UNRESTRICTED
        )
        # Default approval: not required for restricted (allowlist protects),
        # required for unrestricted outside sandboxes (but factory overrides for unrestricted profile)
        self._requires_approval = (
            requires_approval if requires_approval is not None else (not restricted)
        )
        self._allow_parallel = allow_parallel
        self._output_dir: Path | None = None
        self._output_seq = 0

    @property
    def name(self) -> str:
        return "bash"

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    @property
    def parallel_safe(self) -> bool:
        return self._allow_parallel

    @property
    def description(self) -> str:
        if self._restricted:
            return (
                "Execute a shell command to inspect files, logs, or system state. "
                "Use this for text-based inspection with tools like grep, cat, head, "
                "tail, find, jq, yq, etc. Commands are read-only."
            )
        else:
            return (
                "Execute a bash command. Use for running programs, installing packages, "
                "building code, file operations, and any other shell tasks. "
                "For anything that may run longer than the timeout (training, long builds, "
                "servers), set run_in_background: the call returns at once with a PID and a "
                "log file, which you then check with tail and kill -0."
            )

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
        }
        if not self._restricted:
            properties["run_in_background"] = {
                "type": "boolean",
                "description": (
                    "Start the command and return immediately with its PID and log file. "
                    "The timeout does not apply. The exit code is appended to the log "
                    "when the command finishes."
                ),
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

    async def _start_background(
        self, command: str, working_dir: str, start_time: float
    ) -> ToolOutput:
        """Start a command detached from this call and return its PID and log path."""
        log_path = self._new_output_path("bg")
        quoted_log = shlex.quote(str(log_path))
        wrapped = f'( {command}\n) > {quoted_log} 2>&1; echo "[exit code: $?]" >> {quoted_log}'
        process = await asyncio.create_subprocess_shell(
            wrapped,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=working_dir,
            start_new_session=True,
        )
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

        # Check if command is allowed (only in restricted mode)
        if self._restricted:
            allowed, reason = is_command_allowed(command)
            if not allowed:
                return ToolOutput(
                    content=f"Command blocked: {reason}",
                    success=False,
                )
            if background:
                return ToolOutput(
                    content="run_in_background is not available in restricted mode",
                    success=False,
                )

        start_time = time.perf_counter()

        try:
            if background:
                return await self._start_background(command, working_dir, start_time)

            output_path = self._new_output_path("cmd")
            with open(output_path, "wb") as sink:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=sink,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=working_dir,
                    start_new_session=True,
                )

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

            if timed_out:
                output += (
                    f"\n\n[Command timed out after {timeout}s; it and its child processes "
                    f"were killed. For long-running commands use run_in_background.]"
                    if not self._restricted
                    else f"\n\n[Command timed out after {timeout}s and was killed]"
                )
                metadata.update(exit_code=-1, timed_out=True)
                return ToolOutput(
                    content=self._format_output(output, exit_code=-1, duration_seconds=duration),
                    success=False,
                    metadata=metadata,
                )

            exit_code = process.returncode
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
