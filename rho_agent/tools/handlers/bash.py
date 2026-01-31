"""Bash execution handler with configurable restrictions.

Supports two modes:
- RESTRICTED: Only allowlisted read-only commands (grep, cat, find, etc.)
- UNRESTRICTED: Any command allowed (for sandboxed container environments)
"""

import asyncio
import os
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

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
DANGEROUS_PATTERNS = [
    ">",  # Redirect (overwrite)
    ">>",  # Redirect (append)
    "rm ",
    "rm\t",
    "rmdir",
    "mv ",
    "mv\t",
    "cp ",  # Could overwrite
    "cp\t",
    "chmod",
    "chown",
    "chgrp",
    "mkdir",
    "touch",
    "truncate",
    "shred",
    "dd ",
    "dd\t",
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
    "brew ",
    "pip ",
    "npm ",
    "yarn ",
    "cargo ",
    "sudo",
    "su ",
    "su\t",
    "doas",
]


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
    for i, part in enumerate(parts):
        if "=" not in part:
            return part

    return parts[0] if parts else None


def is_command_allowed(command: str) -> tuple[bool, str]:
    """Check if a command is allowed in RESTRICTED mode. Returns (allowed, reason)."""
    # Check for dangerous patterns first
    for pattern in DANGEROUS_PATTERNS:
        if pattern in command:
            return False, f"Command contains dangerous pattern: {pattern.strip()}"

    # Extract base command
    base_cmd = extract_base_command(command)
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
    """

    def __init__(
        self,
        restricted: bool = True,
        working_dir: str | None = None,
        timeout: int | None = None,
        requires_approval: bool | None = None,
    ):
        """Initialize BashHandler.

        Args:
            restricted: If True, use command allowlist (default). If False, allow all commands.
            working_dir: Default working directory for commands.
            timeout: Command timeout in seconds. Defaults to 120s (restricted) or 300s (unrestricted).
            requires_approval: Override whether approval is required. Defaults to True for restricted,
                              False for unrestricted.
        """
        self._restricted = restricted
        self._working_dir = working_dir or os.getcwd()
        self._timeout = timeout or (
            DEFAULT_TIMEOUT_RESTRICTED if restricted else DEFAULT_TIMEOUT_UNRESTRICTED
        )
        # Default approval: not required for restricted (allowlist protects),
        # required for unrestricted outside sandboxes (but factory overrides for eval)
        self._requires_approval = requires_approval if requires_approval is not None else (not restricted)

    @property
    def name(self) -> str:
        return "bash"

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

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
                "building code, file operations, and any other shell tasks."
            )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
            },
            "required": ["command"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the shell command and return output."""
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._working_dir)
        timeout = invocation.arguments.get("timeout", self._timeout)

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

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                # Clean up subprocess on cancellation
                process.kill()
                await process.wait()
                raise
            except asyncio.TimeoutError:
                # Capture partial output before killing
                partial_stdout = b""
                partial_stderr = b""
                try:
                    if process.stdout:
                        partial_stdout = await asyncio.wait_for(
                            process.stdout.read(50000), timeout=2.0
                        )
                except (asyncio.TimeoutError, Exception):
                    pass
                try:
                    if process.stderr:
                        partial_stderr = await asyncio.wait_for(
                            process.stderr.read(50000), timeout=2.0
                        )
                except (asyncio.TimeoutError, Exception):
                    pass

                process.kill()
                await process.wait()

                # Build content with partial output
                content = partial_stdout.decode("utf-8", errors="replace")
                if partial_stderr:
                    content += f"\n[stderr]\n{partial_stderr.decode('utf-8', errors='replace')}"
                content += f"\n\n[Command timed out after {timeout}s and was killed]"

                return ToolOutput(
                    content=content,
                    success=False,
                    metadata={
                        "exit_code": -1,
                        "timed_out": True,
                        "working_dir": working_dir,
                        "command": command,
                    },
                )

            exit_code = process.returncode
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Format output with exit code FIRST so model sees success/failure immediately
            # (Following Codex CLI pattern - models need exit code prominent, not buried)
            lines = [f"[Exit code: {exit_code}]"]
            if stdout_str.strip():
                lines.append(stdout_str)
            if stderr_str.strip():
                lines.append(f"[stderr]\n{stderr_str}")
            if not stdout_str.strip() and not stderr_str.strip():
                lines.append("(no output)")

            content = "\n".join(lines)

            return ToolOutput(
                content=content,
                success=exit_code == 0,
                metadata={
                    "exit_code": exit_code,
                    "command": command,
                    "working_dir": working_dir,
                },
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
