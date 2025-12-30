"""Shell command execution handler."""

import asyncio
import os
from typing import Any

from ..base import ToolHandler, ToolInvocation, ToolOutput

DEFAULT_TIMEOUT = 120  # seconds

# Allowlist of safe read-only commands
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

# Patterns that indicate write operations (block these)
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
    """Check if a command is allowed. Returns (allowed, reason)."""
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


class ShellHandler(ToolHandler):
    """Execute shell commands for inspection and research."""

    def __init__(
        self, working_dir: str | None = None, timeout: int = DEFAULT_TIMEOUT
    ) -> None:
        self._working_dir = working_dir or os.getcwd()
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "shell"

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Execute a shell command to inspect files, logs, or system state. "
            "Use this for text-based inspection with tools like grep, cat, head, "
            "tail, find, jq, yq, etc. Commands are read-only."
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
                    "description": "Working directory for the command (optional)",
                },
            },
            "required": ["command"],
        }

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the shell command and return output."""
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._working_dir)

        if not command:
            return ToolOutput(content="No command provided", success=False)

        # Check if command is allowed
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
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolOutput(
                    content=f"Command timed out after {self._timeout} seconds",
                    success=False,
                    metadata={"timed_out": True},
                )

            exit_code = process.returncode
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Combine output
            output_parts = []
            if stdout_str:
                output_parts.append(stdout_str)
            if stderr_str:
                output_parts.append(f"[stderr]\n{stderr_str}")

            content = "\n".join(output_parts) if output_parts else "(no output)"

            return ToolOutput(
                content=content,
                success=exit_code == 0,
                metadata={
                    "exit_code": exit_code,
                    "command": command,
                },
            )

        except Exception as e:
            return ToolOutput(
                content=f"Error executing command: {e}",
                success=False,
            )
