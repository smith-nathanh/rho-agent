"""Automated verification checks (test, lint, typecheck)."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass

from .models import VerificationConfig


@dataclass
class CheckResult:
    """Result of running verification checks."""

    passed: bool
    output: str  # combined stdout/stderr for failed checks


async def _run_check(cmd: str, working_dir: str) -> tuple[bool, str]:
    """Run a single check command. Returns (passed, output)."""
    # Disallow shell control operators to avoid arbitrary shell composition.
    if any(token in cmd for token in ("&&", "||", ";", "|", ">", "<", "$(", "`")):
        return False, f"Disallowed shell operators in check command: {cmd}"

    argv = shlex.split(cmd)
    if not argv:
        return False, "Empty check command."

    try:
        proc = await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"Command not found: {argv[0]}"

    stdout, _ = await proc.communicate()
    text = stdout.decode() if stdout else ""
    return proc.returncode == 0, text


async def run_checks(
    verification: VerificationConfig,
    working_dir: str,
) -> CheckResult:
    """Run all configured checks. All must pass for passed=True.

    If no commands are configured, returns passed=True.
    """
    commands = [
        ("test", verification.test_cmd),
        ("lint", verification.lint_cmd),
        ("typecheck", verification.typecheck_cmd),
    ]
    active = [(label, cmd) for label, cmd in commands if cmd]
    if not active:
        return CheckResult(passed=True, output="")

    failures: list[str] = []
    for label, cmd in active:
        passed, output = await _run_check(cmd, working_dir)
        if not passed:
            failures.append(f"=== {label}: {cmd} ===\n{output}")

    if failures:
        return CheckResult(passed=False, output="\n".join(failures))
    return CheckResult(passed=True, output="")
