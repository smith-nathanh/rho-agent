"""Remote shell execution via Daytona sandbox."""

from __future__ import annotations

import json
import shlex
import time
from typing import Any

from ....core.truncate import APPROX_BYTES_PER_TOKEN, MAX_OUTPUT_TOKENS
from ...base import ToolHandler, ToolInvocation, ToolOutput
from .manager import SandboxManager

DEFAULT_TIMEOUT = 300
# Directory in the sandbox for saved command output and background logs.
OUTPUT_DIR = "/tmp/rho-agent-bash"
# Output returned inline before the rest is left in a file in the sandbox.
# Half the session limit, leaving room for JSON escaping of the text.
MAX_INLINE_BYTES = MAX_OUTPUT_TOKENS * APPROX_BYTES_PER_TOKEN // 2
# Exit status GNU and BusyBox `timeout` use when the time limit was hit.
TIMEOUT_EXIT_CODE = 124


def build_foreground_script(command: str, timeout: int, seq: int) -> str:
    """Wrap a command so the sandbox enforces the timeout and keeps large output.

    - ``timeout`` (when the image has it) kills the command's whole process
      group, not only the shell, and exits with 124.
    - Output goes to a file. Small output is printed and the file removed;
      large output is printed as head and tail with the file's path.
    """
    quoted = shlex.quote(command)
    half = MAX_INLINE_BYTES // 2
    log = f"{OUTPUT_DIR}/cmd-$$-{seq}.log"
    return (
        f"mkdir -p {OUTPUT_DIR}\n"
        f'f="{log}"\n'
        "if command -v timeout >/dev/null 2>&1; then\n"
        f'  timeout -k 5 {int(timeout)} bash -c {quoted} > "$f" 2>&1 </dev/null\n'
        "else\n"
        f'  bash -c {quoted} > "$f" 2>&1 </dev/null\n'
        "fi\n"
        "ec=$?\n"
        'size=$(wc -c < "$f" | tr -d " ")\n'
        f'if [ "$size" -le {MAX_INLINE_BYTES} ]; then\n'
        '  cat "$f"; rm -f "$f"\n'
        "else\n"
        f'  head -c {half} "$f"\n'
        '  printf "\\n...output truncated...\\n"\n'
        f'  tail -c {half} "$f"\n'
        '  printf "\\n\\n[Output was %s bytes. Full output saved to %s in the sandbox; '
        'read parts of it with grep, head, tail or sed -n.]\\n" "$size" "$f"\n'
        "fi\n"
        "exit $ec"
    )


def build_background_script(command: str, seq: int) -> str:
    """Start a command detached from the call; print its PID and log path."""
    log = f"{OUTPUT_DIR}/bg-$$-{seq}.log"
    inner = f'( {command}\n) > "$0" 2>&1; echo "[exit code: $?]" >> "$0"'
    launcher = "setsid"  # own session, so it survives the exec call ending
    return (
        f"mkdir -p {OUTPUT_DIR}\n"
        f'f="{log}"\n'
        f"if command -v {launcher} >/dev/null 2>&1; then\n"
        f'  {launcher} nohup bash -c {shlex.quote(inner)} "$f" >/dev/null 2>&1 </dev/null &\n'
        "else\n"
        f'  nohup bash -c {shlex.quote(inner)} "$f" >/dev/null 2>&1 </dev/null &\n'
        "fi\n"
        "pid=$!\n"
        'printf "Started in background. PID %s.\\nLog: %s\\n" "$pid" "$f"\n'
        'printf "Check progress: tail -n 40 %s\\n" "$f"\n'
        'printf "Still running?: kill -0 %s && echo running || echo finished\\n" "$pid"\n'
        'printf "Stop it: kill %s\\n" "$pid"\n'
        'printf "The line \\"[exit code: N]\\" is appended to the log when it finishes.\\n"'
    )


class DaytonaBashHandler(ToolHandler):
    """Execute shell commands in a remote Daytona sandbox.

    Standard agentic tool name: 'bash'

    Mirrors the local handler: a timeout kills the command's process group,
    output too large to return inline is kept in a file in the sandbox, and
    ``run_in_background`` starts a command and returns its PID and log path.
    """

    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager
        self._seq = 0

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a bash command in a remote cloud sandbox. Use for running programs, "
            "installing packages, building code, file operations, and any other shell tasks. "
            "For anything that may run longer than the timeout (training, long builds, "
            "servers), set run_in_background: the call returns at once with a PID and a "
            "log file, which you then check with tail and kill -0."
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
                    "description": f"Working directory (default: {self._manager.working_dir})",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default: {DEFAULT_TIMEOUT})",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Start the command and return immediately with its PID and log file. "
                        "The timeout does not apply. The exit code is appended to the log "
                        "when the command finishes."
                    ),
                },
            },
            "required": ["command"],
        }

    @staticmethod
    def _format(output: str, exit_code: int, duration: float) -> str:
        return json.dumps(
            {
                "output": output,
                "metadata": {"exit_code": exit_code, "duration_seconds": round(duration, 1)},
            },
            indent=2,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute a shell command in the remote sandbox."""
        command = invocation.arguments.get("command", "")
        working_dir = invocation.arguments.get("working_dir", self._manager.working_dir)
        timeout = int(invocation.arguments.get("timeout", DEFAULT_TIMEOUT))
        background = bool(invocation.arguments.get("run_in_background", False))

        if not command:
            return ToolOutput(content="No command provided", success=False)

        self._seq += 1
        start_time = time.perf_counter()
        metadata: dict[str, Any] = {"command": command, "working_dir": working_dir}

        try:
            sandbox = await self._manager.get_sandbox()
            if background:
                script = build_background_script(command, self._seq)
                sdk_timeout = 60
            else:
                script = build_foreground_script(command, timeout, self._seq)
                # The sandbox-side `timeout` should fire first; this is a backstop.
                sdk_timeout = timeout + 30
            response = await sandbox.process.exec(script, cwd=working_dir, timeout=sdk_timeout)

            duration = time.perf_counter() - start_time
            output = response.result or ""
            exit_code = response.exit_code
            metadata["duration_seconds"] = round(duration, 1)

            if background:
                metadata["background"] = True
            elif exit_code == TIMEOUT_EXIT_CODE and duration >= timeout - 1:
                output += (
                    f"\n\n[Command timed out after {timeout}s; it and its child processes "
                    f"were killed. For long-running commands use run_in_background.]"
                )
                metadata["timed_out"] = True
                exit_code = -1
            if "Full output saved to" in output:
                metadata["output_saved_in_sandbox"] = True

            metadata["exit_code"] = exit_code
            return ToolOutput(
                content=self._format(output, exit_code, duration),
                success=exit_code == 0,
                metadata=metadata,
            )

        except Exception as e:
            duration = time.perf_counter() - start_time
            # Check for Daytona-specific errors
            if "Timeout" in type(e).__name__ or "timed out" in str(e).lower():
                msg = (
                    f"Command timed out after {timeout}s. "
                    "For long-running commands use run_in_background."
                )
                metadata["timed_out"] = True
            else:
                msg = f"Sandbox execution error: {e}"
            metadata.update(exit_code=-1, duration_seconds=round(duration, 1))
            return ToolOutput(
                content=self._format(msg, -1, duration), success=False, metadata=metadata
            )
