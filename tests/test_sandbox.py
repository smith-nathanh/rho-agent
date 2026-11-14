"""Tests for access levels and the OS sandbox: parsing, resolution, grants,
backend arguments, and (where the platform supports it) commands confined."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from rho_agent import Agent, AgentConfig, Session
from rho_agent.sandbox import (
    DEFAULT_DENY_READ,
    Access,
    AccessLevel,
    Grant,
    ResolvedPolicy,
    Sandbox,
    SandboxUnavailableError,
    bubblewrap,
    check_backend,
    create_sandbox,
    seatbelt,
    seccomp,
)
from rho_agent.sandbox import policy as policy_mod
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.factory import build_registry
from rho_agent.tools.handlers.bash import BashHandler
from rho_agent.tools.handlers.edit import EditHandler
from rho_agent.tools.handlers.grep import GrepHandler
from rho_agent.tools.handlers.read import ReadHandler
from rho_agent.tools.handlers.request_access import RequestAccessHandler
from rho_agent.tools.handlers.write import WriteHandler

BACKEND, BACKEND_ERROR = check_backend()
needs_backend = pytest.mark.skipif(
    BACKEND is None, reason=f"no working sandbox backend: {BACKEND_ERROR}"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _real(p: Path | str) -> str:
    return os.path.realpath(p)


def _session(answers: list[bool] | None = None) -> tuple[Session, list[tuple[str, dict]]]:
    """A session (full access, no model calls) whose approval callback answers
    with ``answers`` in turn and records what it was asked. None: no callback."""
    session = Session(Agent(AgentConfig(access="full", model="m")), client=object())
    asked: list[tuple[str, dict]] = []
    if answers is not None:
        remaining = list(answers)

        async def callback(tool_name: str, tool_args: dict[str, Any]) -> bool:
            asked.append((tool_name, tool_args))
            return remaining.pop(0)

        session.approval_callback = callback
    return session, asked


def _invocation(name: str, session: Session | None = None, **arguments: Any) -> ToolInvocation:
    return ToolInvocation(call_id="c", tool_name=name, arguments=arguments, session=session)


# --- Access parsing ---------------------------------------------------------


def test_default_access_is_workspace(monkeypatch) -> None:
    monkeypatch.delenv("RHO_AGENT_ACCESS", raising=False)
    assert Access().level == AccessLevel.WORKSPACE
    assert AgentConfig(model="m").access.level == AccessLevel.WORKSPACE


def test_access_from_env(monkeypatch) -> None:
    monkeypatch.setenv("RHO_AGENT_ACCESS", "read-only")
    assert AgentConfig(model="m").access.level == AccessLevel.READ_ONLY


def test_access_from_string_and_mapping() -> None:
    assert Access.from_value("full").sandboxed is False
    access = Access.from_value({"level": "read-only", "network": "off", "add_dirs": ["~/x"]})
    assert access.level == AccessLevel.READ_ONLY
    assert access.network is False
    assert access.add_dirs == ("~/x",)
    assert access.unix_sockets is False
    assert Access.from_value({"unix_sockets": "on"}).unix_sockets is True
    assert Access(unix_sockets=True).to_value() == {"level": "workspace", "unix_sockets": True}
    assert Access(level="workspace").level == AccessLevel.WORKSPACE  # plain strings accepted


def test_access_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        Access.from_value("everything")
    with pytest.raises(ValueError, match="Unknown access settings"):
        Access.from_value({"level": "workspace", "netwrok": False})
    with pytest.raises(ValueError):
        Access.from_value({"network": "sometimes"})


def test_default_keyword_extends_lists() -> None:
    access = Access.from_value({"deny_read": ["default", "~/secrets"], "env_scrub": "none"})
    assert access.deny_read == (*DEFAULT_DENY_READ, "~/secrets")
    assert access.env_scrub == ()


def test_access_round_trips_through_config() -> None:
    config = AgentConfig(model="m", access=Access("read-only", network=False))
    data = config._to_dict()
    assert data["access"] == {"level": "read-only", "network": False}
    assert AgentConfig._from_dict(data).access == config.access
    assert AgentConfig(model="m", access="full")._to_dict()["access"] == "full"


# --- resolution -------------------------------------------------------------


def test_workspace_roots(tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    policy = Access(add_dirs=(str(extra),)).resolve(tmp_path / "ws")
    assert _real(tmp_path / "ws") in policy.writable
    assert _real(extra) in policy.writable
    assert policy.extra_roots == (_real(extra),)
    for temp in policy_mod.temp_dirs():
        assert temp in policy.writable


def test_read_only_writes_temp_only(tmp_path: Path) -> None:
    policy = Access("read-only").resolve(tmp_path)
    assert set(policy.writable) == set(policy_mod.temp_dirs())
    assert policy.deny_write == ()


def test_git_dirs_of_plain_repo_from_subdirectory(tmp_path: Path) -> None:
    _git("init", "-q", str(tmp_path), cwd=tmp_path)
    sub = tmp_path / "src"
    sub.mkdir()
    policy = Access().resolve(sub)
    git_dir = _real(tmp_path / ".git")
    assert git_dir in policy.writable
    assert os.path.join(git_dir, "hooks") in policy.deny_write
    assert os.path.join(git_dir, "config") in policy.deny_write


def test_git_dirs_of_worktree_include_main_repo(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature", cwd=main)

    dirs = policy_mod.git_dirs_for(_real(tmp_path / "wt"))
    assert _real(main / ".git" / "worktrees" / "wt") in dirs
    assert _real(main / ".git") in dirs

    policy = Access().resolve(tmp_path / "wt")
    assert _real(main / ".git" / "hooks") in policy.deny_write
    assert _real(main / ".git" / "config") in policy.deny_write


def test_relative_and_absolute_deny_write(tmp_path: Path) -> None:
    policy = Access(deny_write=(".env", ".gitignore", str(tmp_path / "locked"))).resolve(tmp_path)
    assert _real(tmp_path / ".env") in policy.deny_write
    assert _real(tmp_path / ".gitignore") in policy.deny_write  # not a .git/ entry
    assert _real(tmp_path / "locked") in policy.deny_write


def _policy(tmp_path: Path) -> tuple[ResolvedPolicy, Path, Path, Path]:
    ws, outside, secret = tmp_path / "ws", tmp_path / "outside", tmp_path / "secret"
    for d in (ws, outside, secret):
        d.mkdir()
    (ws / "escape").symlink_to(outside)
    policy = ResolvedPolicy(
        level=AccessLevel.WORKSPACE,
        working_dir=_real(ws),
        writable=(_real(ws),),
        deny_write=(_real(ws / ".env"),),
        deny_read=(_real(secret),),
        network=False,
    )
    return policy, ws, outside, secret


def test_can_write_and_read(tmp_path: Path) -> None:
    policy, ws, outside, secret = _policy(tmp_path)
    assert policy.can_write(ws / "a.txt")[0] is True
    assert policy.can_write(ws / "new" / "dir" / "b.txt")[0] is True
    assert policy.can_write(outside / "a.txt")[0] is False
    assert policy.can_write(ws / "escape" / "a.txt")[0] is False  # through a symlink
    assert policy.can_write(ws / ".env")[0] is False
    assert policy.can_read(ws / "a.txt")[0] is True
    assert policy.can_read(secret / "key")[0] is False
    assert policy.can_write(secret / "key")[0] is False


def test_is_within_handles_prefix_names() -> None:
    assert policy_mod.is_within("/a/b", "/a/b")
    assert policy_mod.is_within("/a/b/c", "/a/b")
    assert not policy_mod.is_within("/a/bc", "/a/b")
    assert policy_mod.is_within("/anything", "/")


def test_child_env_scrubs_secrets() -> None:
    policy = Access("read-only").resolve("/")
    env = policy.child_env(
        {"PATH": "/bin", "OPENAI_API_KEY": "x", "RHO_AGENT_MODEL": "m", "AWS_SESSION_TOKEN": "t"}
    )
    assert env == {"PATH": "/bin", "RHO_AGENT_SANDBOX": "read-only"}


def test_describe_mentions_limits(tmp_path: Path) -> None:
    description = Access(network=False).resolve(tmp_path).describe()
    assert "workspace" in description
    assert "blocked" in description
    assert "~/.ssh" in description


# --- grants -----------------------------------------------------------------


def test_plan_grant(tmp_path: Path) -> None:
    policy, ws, outside, secret = _policy(tmp_path)
    grant, _ = policy.plan_grant(outside, write=True)
    assert grant == Grant(_real(outside), write=True)
    # Reads of a protected path are granted for the whole protected entry.
    assert policy.plan_grant(secret / "key", write=False)[0] == Grant(_real(secret), write=False)
    assert policy.plan_grant(ws, write=True) == (None, f"{_real(ws)} is already writable.")
    assert policy.plan_grant(outside, write=False)[0] is None  # already readable
    grant, why = policy.plan_grant(ws / ".env", write=True)
    assert grant is None
    assert "protected" in why


def test_with_grants(tmp_path: Path) -> None:
    policy, _, outside, secret = _policy(tmp_path)
    granted = policy.with_grants([Grant(_real(outside)), Grant(_real(secret), write=False)])
    assert granted.can_write(outside / "a.txt")[0] is True
    assert granted.can_read(secret / "key")[0] is True
    assert granted.can_write(secret / "key")[0] is False  # read grant only
    assert granted.extra_roots == (_real(outside),)
    assert policy.can_write(outside / "a.txt")[0] is False  # the original is unchanged


def test_write_grant_on_repo_protects_its_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    policy = Access().resolve(tmp_path / "ws").with_grants([Grant(_real(repo))])
    assert policy.can_write(repo / "file.py")[0] is True
    assert policy.can_write(repo / ".git" / "objects" / "x")[0] is True
    assert policy.can_write(repo / ".git" / "hooks" / "pre-commit")[0] is False


def test_grants_are_per_session(tmp_path: Path) -> None:
    policy, _, outside, _ = _policy(tmp_path)
    sandbox = Sandbox(policy, "seatbelt")
    one = sandbox.with_grants([Grant(_real(outside))])
    assert one.check_write(outside / "x")[0] is True
    assert sandbox.check_write(outside / "x")[0] is False
    assert sandbox.with_grants([Grant(_real(outside))]) is one  # cached


# --- backend selection ------------------------------------------------------


def test_full_access_has_no_sandbox(tmp_path: Path) -> None:
    assert create_sandbox("full", tmp_path) is None


def test_create_sandbox_fails_closed(monkeypatch, tmp_path: Path) -> None:
    import rho_agent.sandbox as sandbox_pkg

    monkeypatch.setattr(sandbox_pkg, "check_backend", lambda: (None, "no backend here"))
    with pytest.raises(SandboxUnavailableError, match="no backend here"):
        create_sandbox("read-only", tmp_path)
    with pytest.raises(SandboxUnavailableError):
        Agent(AgentConfig(model="m", access="workspace", working_dir=str(tmp_path)))


# --- backend arguments (pure, any platform) ----------------------------------


def _backend_policy(tmp_path: Path, network: bool = False) -> ResolvedPolicy:
    ws = tmp_path / "ws"
    hooks = ws / ".git" / "hooks"
    secret = tmp_path / "secret"
    hooks.mkdir(parents=True)
    secret.mkdir()
    (tmp_path / "token").write_text("t")
    return ResolvedPolicy(
        level=AccessLevel.WORKSPACE,
        working_dir=_real(ws),
        writable=(_real(ws), _real(tmp_path / "missing")),
        deny_write=(_real(hooks),),
        deny_read=(_real(secret), _real(tmp_path / "token")),
        network=network,
    )


def test_seatbelt_profile(tmp_path: Path) -> None:
    policy = _backend_policy(tmp_path)
    text, params = seatbelt.build_profile(policy)
    assert text.startswith("(version 1)")
    assert '(require-not (subpath (param "W0")))' in text
    assert '(deny file-write* (subpath (param "DW0")))' in text
    assert "(deny file-read*" in text
    assert "(deny network-outbound (remote ip))" in text
    assert f"W0={_real(tmp_path / 'ws')}" in params
    assert "(deny network-outbound (remote unix-socket))" in text
    assert "/private/var/run/mDNSResponder" in text  # DNS still works
    networked, _ = seatbelt.build_profile(_backend_policy(tmp_path / "n", network=True))
    assert "(remote ip)" not in networked
    sockets = replace(_backend_policy(tmp_path / "s"), unix_sockets=True)
    assert "unix-socket" not in seatbelt.build_profile(sockets)[0]


def _run_bpf(program: bytes, nr: int, arch: int, arg0: int = 0) -> int:
    """Evaluate the classic-BPF subset ``seccomp.build_filter`` emits."""
    data = struct.pack("<IIQ6Q", nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    ins = [struct.unpack_from("<HBBI", program, i) for i in range(0, len(program), 8)]
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        pc += 1
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
        elif code in (0x15, 0x35):
            hit = acc == k if code == 0x15 else acc >= k
            pc += jt if hit else jf
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")


@pytest.mark.parametrize(
    ("machine", "arch", "nr_socket"),
    [("x86_64", 0xC000003E, 41), ("aarch64", 0xC00000B7, 198)],
)
def test_seccomp_filter_blocks_unix_sockets(machine: str, arch: int, nr_socket: int) -> None:
    program = seccomp.build_filter(machine)
    allow, eperm = 0x7FFF0000, 0x00050001
    assert _run_bpf(program, nr_socket, arch, arg0=1) == eperm  # AF_UNIX
    assert _run_bpf(program, nr_socket, arch, arg0=2) == allow  # AF_INET
    assert _run_bpf(program, nr_socket, arch, arg0=10) == allow  # AF_INET6
    assert _run_bpf(program, 425, arch) == eperm  # io_uring_setup
    assert _run_bpf(program, 0, arch) == allow  # read / io_setup
    assert _run_bpf(program, nr_socket, 0x40000003, arg0=1) == eperm  # 32-bit
    if machine == "x86_64":
        assert _run_bpf(program, 0x40000000 | nr_socket, arch, arg0=1) == eperm  # x32


def test_bubblewrap_wrap_loads_seccomp(tmp_path: Path) -> None:
    policy = _backend_policy(tmp_path)
    argv = bubblewrap.wrap(policy, ["echo", "hi"])
    assert argv[:3] == ["/bin/sh", "-c", 'exec "$@" 3<"$0"']
    path = argv[3]
    assert Path(path).read_bytes() == seccomp.build_filter()
    directory = os.path.dirname(path)
    assert argv[-8:] == ["--ro-bind", directory, directory, "--seccomp", "3", "--", "echo", "hi"]
    plain = bubblewrap.wrap(replace(policy, unix_sockets=True), ["echo"])
    assert "--seccomp" not in plain and plain[0] != "/bin/sh"


def test_bubblewrap_args(tmp_path: Path) -> None:
    policy = _backend_policy(tmp_path)
    args = bubblewrap.build_args(policy)
    joined = " ".join(args)
    assert args[:2] == ["--die-with-parent", "--unshare-pid"]
    assert "--ro-bind / /" in joined
    assert f"--bind {_real(tmp_path / 'ws')} {_real(tmp_path / 'ws')}" in joined
    assert "missing" not in joined  # nonexistent writable roots are skipped
    assert f"--ro-bind {policy.deny_write[0]} {policy.deny_write[0]}" in joined
    assert f"--tmpfs {_real(tmp_path / 'secret')}" in joined
    assert f"--ro-bind /dev/null {_real(tmp_path / 'token')}" in joined
    assert "--unshare-net" in args
    with patch.object(bubblewrap, "_supports_perms", lambda: True):
        joined = " ".join(bubblewrap.build_args(policy))
    assert f"--perms 0000 --tmpfs {_real(tmp_path / 'secret')}" in joined
    networked = bubblewrap.build_args(_backend_policy(tmp_path / "n", network=True))
    assert "--unshare-net" not in networked


# --- request_access and the approval flow (no backend needed) ----------------


async def test_request_access_grants_for_the_session(tmp_path: Path) -> None:
    policy, ws, outside, _ = _policy(tmp_path)
    handler = RequestAccessHandler(Sandbox(policy, "seatbelt"), working_dir=str(ws))
    session, asked = _session([True])
    out = await handler.handle(
        _invocation("request_access", session, path=str(outside), reason="fix shared lib")
    )
    assert out.success, out.content
    assert asked == [
        ("request_access", {"path": _real(outside), "access": "write", "reason": "fix shared lib"})
    ]
    assert session.access_grants == [Grant(_real(outside))]
    # Asking again is answered without bothering the user.
    again = await handler.handle(_invocation("request_access", session, path=str(outside)))
    assert again.success and "already writable" in again.content
    assert len(asked) == 1


async def test_request_access_declined(tmp_path: Path) -> None:
    policy, ws, outside, _ = _policy(tmp_path)
    handler = RequestAccessHandler(Sandbox(policy, "seatbelt"), working_dir=str(ws))
    session, _ = _session([False])
    out = await handler.handle(_invocation("request_access", session, path=str(outside)))
    assert not out.success
    assert "declined" in out.content
    assert session.access_grants == []


async def test_request_access_refuses_protected_paths(tmp_path: Path) -> None:
    policy, ws, _, _ = _policy(tmp_path)
    handler = RequestAccessHandler(Sandbox(policy, "seatbelt"), working_dir=str(ws))
    session, asked = _session([True])
    out = await handler.handle(_invocation("request_access", session, path=".env"))
    assert not out.success
    assert asked == []


def test_request_access_hidden_without_an_approver(tmp_path: Path) -> None:
    session, _ = _session(None)
    session._registry.register(RequestAccessHandler(Sandbox(_policy(tmp_path)[0], "seatbelt")))
    names = [s["function"]["name"] for s in session._tool_specs()]
    assert "request_access" not in names

    async def yes(*_: Any) -> bool:
        return True

    session.approval_callback = yes
    assert "request_access" in [s["function"]["name"] for s in session._tool_specs()]


async def test_write_outside_asks_for_the_directory(tmp_path: Path) -> None:
    policy, ws, outside, _ = _policy(tmp_path)
    handler = WriteHandler(working_dir=str(ws), sandbox=Sandbox(policy, "seatbelt"))
    session, asked = _session([True])
    out = await handler.handle(
        _invocation("write", session, path=str(outside / "a.txt"), content="x")
    )
    assert out.success, out.content
    assert asked[0][1]["path"] == _real(outside)
    assert session.access_grants == [Grant(_real(outside))]
    # The grant covers later writes there.
    out = await handler.handle(
        _invocation("write", session, path=str(outside / "b.txt"), content="y")
    )
    assert out.success and len(asked) == 1


async def test_write_outside_without_approver_is_refused(tmp_path: Path) -> None:
    policy, ws, outside, _ = _policy(tmp_path)
    handler = WriteHandler(working_dir=str(ws), sandbox=Sandbox(policy, "seatbelt"))
    out = await handler.handle(
        _invocation("write", None, path=str(outside / "no.txt"), content="x")
    )
    assert not out.success
    assert "Sandbox" in out.content
    assert not (outside / "no.txt").exists()


async def test_edit_outside_declined(tmp_path: Path) -> None:
    policy, ws, outside, _ = _policy(tmp_path)
    (outside / "f.txt").write_text("old")
    handler = EditHandler(working_dir=str(ws), sandbox=Sandbox(policy, "seatbelt"))
    session, _ = _session([False])
    out = await handler.handle(
        _invocation(
            "edit", session, path=str(outside / "f.txt"), old_string="old", new_string="new"
        )
    )
    assert not out.success
    assert "declined" in out.content
    assert (outside / "f.txt").read_text() == "old"


async def test_read_and_grep_refuse_denied_paths(tmp_path: Path) -> None:
    policy, ws, _, secret = _policy(tmp_path)
    (secret / "key").write_text("hunter2")
    sandbox = Sandbox(policy, "seatbelt")
    read = await ReadHandler(working_dir=str(ws), sandbox=sandbox).handle(
        _invocation("read", None, path=str(secret / "key"))
    )
    assert not read.success and "hunter2" not in read.content
    grep = await GrepHandler(working_dir=str(ws), sandbox=sandbox).handle(
        _invocation("grep", None, pattern="hunter", path=str(secret))
    )
    assert not grep.success and "hunter2" not in grep.content


async def test_read_grant_unlocks_protected_path(tmp_path: Path) -> None:
    policy, ws, _, secret = _policy(tmp_path)
    (secret / "key").write_text("hunter2")
    session, _ = _session(None)
    session.access_grants = [Grant(_real(secret), write=False)]
    read = await ReadHandler(working_dir=str(ws), sandbox=Sandbox(policy, "seatbelt")).handle(
        _invocation("read", session, path=str(secret / "key"))
    )
    assert read.success and "hunter2" in read.content


# --- enforcement (needs a working backend) ----------------------------------


def _confined(tmp_path: Path, network: bool = False) -> tuple[Sandbox, Path, Path]:
    """A sandbox whose only writable root is ``ws`` (not even temp dirs)."""
    ws, outside, secret = tmp_path / "ws", tmp_path / "outside", tmp_path / "secret"
    for d in (ws, outside, secret):
        d.mkdir()
    (secret / "key").write_text("hunter2")
    policy = ResolvedPolicy(
        level=AccessLevel.WORKSPACE,
        working_dir=_real(ws),
        writable=(_real(ws),),
        deny_write=(_real(ws / "protected"),),
        deny_read=(_real(secret),),
        network=network,
    )
    assert BACKEND is not None
    return Sandbox(policy, BACKEND), ws, outside


async def _bash(handler: BashHandler, session: Session | None = None, **arguments):
    out = await handler.handle(_invocation("bash", session, **arguments))
    return out, json.loads(out.content) if out.content.startswith("{") else None


@needs_backend
async def test_bash_writes_only_inside_workspace(tmp_path: Path) -> None:
    sandbox, ws, outside = _confined(tmp_path)
    (ws / "protected").mkdir()
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)

    out, _ = await _bash(handler, command="echo hi > inside.txt && cat inside.txt")
    assert out.success
    assert (ws / "inside.txt").read_text() == "hi\n"
    assert out.metadata["sandbox"] == "workspace"

    out, body = await _bash(handler, command=f"echo hi > {outside / 'x.txt'}")
    assert not out.success
    assert not (outside / "x.txt").exists()
    assert "[Sandbox (workspace)" in body["output"]
    assert "request_access" not in body["output"]  # nobody to ask

    await _bash(handler, command=f"sh -c 'touch {outside / 'nested'}'")
    assert not (outside / "nested").exists()

    await _bash(handler, command="touch protected/x")
    assert not (ws / "protected" / "x").exists()


@needs_backend
async def test_bash_uses_session_grants(tmp_path: Path) -> None:
    sandbox, ws, outside = _confined(tmp_path)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    session, _ = _session([])
    out, body = await _bash(handler, session, command=f"touch {outside / 'x'}")
    assert not out.success
    assert "request_access" in body["output"]  # someone can answer
    session.access_grants = [Grant(_real(outside))]
    out, _ = await _bash(handler, session, command=f"touch {outside / 'x'}")
    assert out.success
    assert (outside / "x").exists()


@needs_backend
async def test_bash_unsandboxed_needs_approval(tmp_path: Path) -> None:
    sandbox, ws, outside = _confined(tmp_path)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    command = f"touch {outside / 'y'}"

    out, _ = await _bash(handler, None, command=command, unsandboxed=True)
    assert not out.success and "Nobody is available" in out.content

    session, asked = _session([False, True])
    out, _ = await _bash(handler, session, command=command, unsandboxed=True, reason="r")
    assert not out.success and "declined" in out.content
    assert not (outside / "y").exists()

    out, body = await _bash(handler, session, command=command, unsandboxed=True, reason="r")
    assert out.success, body
    assert (outside / "y").exists()
    assert out.metadata["sandbox"] == "off"
    assert asked[0] == (
        "bash",
        {"command": command, "working_dir": str(ws), "unsandboxed": True, "reason": "r"},
    )


@pytest.mark.parametrize("allowed", [False, True])
@needs_backend
async def test_unix_sockets(tmp_path: Path, allowed: bool) -> None:
    sandbox, ws, _ = _confined(tmp_path)
    if allowed:
        sandbox = Sandbox(replace(sandbox.policy, unix_sockets=True), sandbox.backend)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    # A service listening outside the sandbox, like Docker or ssh-agent.
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tempfile.mkdtemp(prefix="rho-t-") + "/s"  # short: macOS caps socket paths
    server.bind(path)
    server.listen(1)
    try:
        _, body = await _bash(
            handler,
            command=f"python3 -c \"import socket; socket.socket(socket.AF_UNIX).connect('{path}'); "
            "print('connected')\"",
        )
    finally:
        server.close()
        os.unlink(path)
    assert ("connected" in body["output"].splitlines()) is allowed
    if not allowed:
        assert "Unix sockets" in body["output"]  # the hint says why
    # Socket pairs inside the sandbox keep working (asyncio needs them).
    out, body = await _bash(
        handler, command="python3 -c \"import asyncio; asyncio.run(asyncio.sleep(0)); print('ok')\""
    )
    assert out.success, body


@needs_backend
async def test_bash_cannot_read_denied_paths(tmp_path: Path) -> None:
    sandbox, ws, _ = _confined(tmp_path)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    _, body = await _bash(handler, command=f"cat {tmp_path / 'secret' / 'key'}")
    assert "hunter2" not in body["output"]
    # Listing the directory is refused too (not shown as empty), with a hint.
    out, body = await _bash(handler, command=f"ls {tmp_path / 'secret'}")
    assert not out.success
    assert "key" not in body["output"].split("[Sandbox")[0]
    assert "[Sandbox (workspace)" in body["output"]


@needs_backend
async def test_bash_background_is_confined(tmp_path: Path) -> None:
    sandbox, ws, outside = _confined(tmp_path)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    out, _ = await _bash(
        handler, command=f"echo started; touch {outside / 'bg'}", run_in_background=True
    )
    log = Path(out.metadata["log_path"])
    for _ in range(50):
        if "[exit code:" in log.read_text():
            break
        await asyncio.sleep(0.1)
    text = log.read_text()
    assert "started" in text
    assert "[exit code: 1]" in text
    assert not (outside / "bg").exists()


@needs_backend
@pytest.mark.parametrize("network", [False, True])
async def test_bash_network_switch(tmp_path: Path, network: bool) -> None:
    sandbox, ws, _ = _confined(tmp_path, network=network)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        _, body = await _bash(
            handler,
            command=(
                'python3 -c "import socket; '
                f"socket.create_connection(('127.0.0.1', {port}), 3); print('connected')\""
            ),
        )
    finally:
        server.close()
    # Match the printed line exactly: a traceback quotes the source, which also says it.
    assert ("connected" in body["output"].splitlines()) is network


@needs_backend
async def test_bash_env_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOME_API_KEY", "leak")
    sandbox, ws, _ = _confined(tmp_path)
    handler = BashHandler(working_dir=str(ws), sandbox=sandbox)
    _, body = await _bash(handler, command="echo ${SOME_API_KEY:-none} $RHO_AGENT_SANDBOX")
    assert body["output"].strip() == "none workspace"


@needs_backend
async def test_git_commit_in_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature", cwd=main)
    registry = build_registry("workspace", working_dir=str(tmp_path / "wt"))
    handler = registry.get("bash")
    out, body = await _bash(
        handler,
        command="echo x > f && git add f && git -c user.name=t -c user.email=t@t commit -qm f",
    )
    assert out.success, body["output"]
    out, _ = await _bash(handler, command="git config rho.test 1")
    assert not out.success


def test_default_prompt_describes_access(tmp_path: Path) -> None:
    from rho_agent.cli.main_cmd import _default_prompt

    text, _ = _default_prompt(Access(network=False), str(tmp_path), is_remote=False)
    assert "**workspace** access" in text
    assert "request_access" in text
    assert "Network access: blocked" in text

    full, _ = _default_prompt(Access("full"), str(tmp_path), is_remote=False)
    assert "**full** access" in full
    assert "request_access" not in full

    read_only, _ = _default_prompt(Access("read-only"), str(tmp_path), is_remote=False)
    assert "File Editing Tools" not in read_only


def test_monitor_shows_access_requests_plainly(monkeypatch, tmp_path: Path) -> None:
    from rho_agent.cli.formatting import _format_tool_signature

    monkeypatch.setenv("HOME", str(tmp_path))
    args = {"path": str(tmp_path / "proj" / "lib"), "access": "write", "reason": "fix the import"}
    assert _format_tool_signature("request_access", args) == (
        "write access to ~/proj/lib: fix the import"
    )
    bash = {"command": "docker ps", "unsandboxed": True, "reason": "needs Docker"}
    assert _format_tool_signature("bash", bash) == "bash(unsandboxed: docker ps)"
