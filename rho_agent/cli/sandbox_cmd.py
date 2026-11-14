"""`rho-agent sandbox`: inspect and try the OS sandbox."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ..sandbox import Access, AccessLevel, Sandbox, check_backend
from .formatting import _markup
from .state import app, console
from .theme import THEME

sandbox_app = typer.Typer(
    help="Inspect and try the OS sandbox that confines agent commands.",
    no_args_is_help=True,
)
app.add_typer(sandbox_app, name="sandbox")

AccessOption = Annotated[
    str,
    typer.Option("--access", "-a", help="Access level to show: read-only or workspace"),
]
NetworkOption = Annotated[
    bool | None,
    typer.Option("--network/--no-network", help="Override network access"),
]
AddDirOption = Annotated[
    list[str] | None,
    typer.Option("--add-dir", help="Also make this directory writable (repeatable)"),
]
WorkingDirOption = Annotated[
    str | None,
    typer.Option("--working-dir", "-w", help="Working directory (default: current)"),
]


def _access(level: str, network: bool | None, add_dirs: list[str] | None) -> Access:
    """The access to show, from the command-line options."""
    try:
        access = Access(level=AccessLevel(level))
    except ValueError as err:
        console.print(_markup(f"Invalid access {level!r}: use read-only or workspace", THEME.error))
        raise typer.Exit(1) from err
    if not access.sandboxed:
        console.print(_markup("Full access has no sandbox; nothing to show.", THEME.warning))
        raise typer.Exit(1)
    if network is not None:
        access = replace(access, network=network)
    if add_dirs:
        access = replace(access, add_dirs=tuple(add_dirs))
    return access


def _backend_or_exit() -> str:
    backend, error = check_backend()
    if backend is None:
        console.print(_markup(f"Sandbox unavailable: {error}", THEME.error))
        raise typer.Exit(1)
    return backend


@sandbox_app.command()
def check(
    access: AccessOption = "workspace",
    network: NetworkOption = None,
    add_dir: AddDirOption = None,
    working_dir: WorkingDirOption = None,
) -> None:
    """Show the sandbox backend and what it allows, then test that it holds."""
    backend = _backend_or_exit()
    policy = _access(access, network, add_dir)
    cwd = os.path.realpath(working_dir or os.getcwd())

    console.print(_markup(f"Backend: {backend} ({sys.platform})", THEME.success))
    console.print(Sandbox(policy.resolve(cwd), backend).describe())
    console.print()

    results = _self_test(policy, backend)
    table = Table(show_header=True, header_style=THEME.muted, box=None, pad_edge=False)
    table.add_column("")
    table.add_column("Check")
    table.add_column("Detail", style=THEME.muted)
    failed = 0
    for name, ok, detail in results:
        if ok is None:
            mark = _markup("skip", THEME.muted)
        elif ok:
            mark = _markup("pass", THEME.success)
        else:
            mark = _markup("FAIL", THEME.error)
            failed += 1
        table.add_row(mark, name, detail)
    console.print(table)
    if failed:
        console.print(_markup(f"{failed} check(s) failed.", THEME.error))
        raise typer.Exit(1)


@sandbox_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(
    command: Annotated[list[str], typer.Argument(help="Command and arguments, after --")],
    access: AccessOption = "workspace",
    network: NetworkOption = None,
    add_dir: AddDirOption = None,
    working_dir: WorkingDirOption = None,
) -> None:
    """Run a command inside the sandbox, e.g. `rho-agent sandbox run -- touch ~/x`."""
    backend = _backend_or_exit()
    cwd = os.path.realpath(working_dir or os.getcwd())
    sandbox = Sandbox(_access(access, network, add_dir).resolve(cwd), backend)
    argv = command
    if len(command) == 1 and " " in command[0]:
        argv = ["/bin/sh", "-c", command[0]]
    result = subprocess.run(sandbox.wrap(argv), cwd=cwd, env=sandbox.env())
    raise typer.Exit(result.returncode)


def _socket_check(
    policy: Access,
    sh: Callable[[str], subprocess.CompletedProcess[str]],
    last_line: Callable[[subprocess.CompletedProcess[str]], str],
    python: str,
) -> tuple[str, bool | None, str]:
    """Listen on a Unix socket outside the sandbox and try to connect to it,
    as a command would to Docker or ssh-agent."""
    directory = tempfile.mkdtemp(prefix="rho-sock-")
    path = os.path.join(directory, "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        server.listen(1)
        proc = sh(
            f'{python} -c "import socket; s = socket.socket(socket.AF_UNIX); '
            f"s.connect({path!r}); print('connected')\""
        )
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)
    connected = "connected" in proc.stdout
    if policy.unix_sockets:
        return ("local services (Unix sockets) allowed", connected, last_line(proc))
    return ("local services (Unix sockets) blocked", not connected, last_line(proc))


def _self_test(policy: Access, backend: str) -> list[tuple[str, bool | None, str]]:
    """Run probe commands under ``policy`` in a scratch workspace.

    Returns ``(check, passed, detail)`` rows; ``passed`` is None for skipped checks.
    """
    results: list[tuple[str, bool | None, str]] = []
    workspace = Path(tempfile.mkdtemp(prefix="rho-sandbox-check-"))
    home_probe = Path.home() / f".rho-agent-sandbox-probe-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(["git", "init", "-q", str(workspace)], capture_output=True)
        sandbox = Sandbox(policy.resolve(workspace), backend)
        env = sandbox.env({**os.environ, "RHO_SANDBOX_PROBE_API_KEY": "secret"})

        def sh(cmd: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                sandbox.wrap(["/bin/sh", "-c", cmd]),
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        def last_line(proc: subprocess.CompletedProcess[str]) -> str:
            lines = (proc.stdout + proc.stderr).strip().splitlines()
            return lines[-1][:100] if lines else f"exit {proc.returncode}"

        writes_workspace = policy.level == AccessLevel.WORKSPACE
        proc = sh("echo ok > probe.txt && cat probe.txt")
        results.append(
            (
                "write in working directory "
                + ("allowed" if writes_workspace else "allowed (scratch dir is in temp)"),
                proc.returncode == 0,
                last_line(proc),
            )
        )

        proc = sh(f"touch {home_probe}")
        leaked = home_probe.exists()
        results.append(("write to home directory blocked", not leaked, last_line(proc)))

        proc = sh(f"sh -c 'sh -c \"touch {home_probe}.nested\"'")
        nested = Path(f"{home_probe}.nested")
        results.append(("child processes are confined", not nested.exists(), last_line(proc)))
        nested.unlink(missing_ok=True)

        if writes_workspace and (workspace / ".git" / "hooks").is_dir():
            proc = sh("echo 'echo hi' > .git/hooks/pre-commit")
            planted = (workspace / ".git" / "hooks" / "pre-commit").exists()
            results.append(("git hooks protected", not planted, last_line(proc)))

        unreadable = [p for p in sandbox.policy.deny_read if os.path.exists(p)]
        if unreadable:
            target = unreadable[0]
            proc = sh(f"ls {target!r} >/dev/null && echo readable")
            results.append((f"{target} unreadable", "readable" not in proc.stdout, last_line(proc)))
        else:
            results.append(("protected paths unreadable", None, "none of them exist here"))

        proc = sh("echo ${RHO_SANDBOX_PROBE_API_KEY:-scrubbed}")
        scrub_expected = (
            "scrubbed"
            if any(p in ("*_API_KEY", "RHO_SANDBOX_PROBE_API_KEY") for p in policy.env_scrub)
            else "secret"
        )
        results.append(
            (
                "API keys removed from environment",
                proc.stdout.strip() == scrub_expected,
                proc.stdout.strip(),
            )
        )

        python = shutil.which("python3") or sys.executable
        net_cmd = (
            f"{python} -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3); "
            "print('connected')\""
        )
        proc = sh(net_cmd)
        connected = "connected" in proc.stdout
        if policy.network:
            results.append(
                (
                    "network allowed",
                    True if connected else None,
                    "connected" if connected else "no connection (offline?)",
                )
            )
        else:
            results.append(("network blocked", not connected, last_line(proc)))

        results.append(_socket_check(policy, sh, last_line, python))
    finally:
        for probe in (home_probe, Path(f"{home_probe}.nested")):
            probe.unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
    return results
