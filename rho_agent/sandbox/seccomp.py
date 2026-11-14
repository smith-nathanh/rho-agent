"""Linux: a seccomp filter that stops sandboxed commands creating Unix sockets.

bubblewrap cannot filter socket connections by path, and a read-only mount
does not stop ``connect()`` to a socket file. Without this, a command could
reach any local service running outside the sandbox (Docker, ssh-agent, tmux,
the D-Bus session bus) and have it act for it.

The filter makes ``socket(AF_UNIX, ...)`` fail with EPERM. ``socketpair()``
stays allowed: it only links processes inside the sandbox, and Python's
asyncio, among others, needs it. ``io_uring_setup`` is refused too, since
io_uring can create sockets without the ``socket`` system call.

The program is classic BPF, built here so there is nothing to compile. bwrap
loads it from a file descriptor (``--seccomp FD``).
"""

from __future__ import annotations

import atexit
import functools
import os
import platform
import shutil
import struct
import tempfile

# struct seccomp_data offsets: nr (u32), arch (u32), ip (u64), args[6] (u64).
_NR, _ARCH, _ARG0 = 0, 4, 16

# Instruction codes.
_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_RET_K = 0x06  # BPF_RET | BPF_K

_ALLOW = 0x7FFF0000  # SECCOMP_RET_ALLOW
_EPERM = 0x00050000 | 1  # SECCOMP_RET_ERRNO | EPERM

_AF_UNIX = 1
_X32_SYSCALL_BIT = 0x40000000

# machine -> (AUDIT_ARCH_*, socket nr, io_uring_setup nr)
_ARCHES = {
    "x86_64": (0xC000003E, 41, 425),
    "amd64": (0xC000003E, 41, 425),
    "aarch64": (0xC00000B7, 198, 425),
    "arm64": (0xC00000B7, 198, 425),
}


def supported() -> bool:
    return platform.machine().lower() in _ARCHES


def build_filter(machine: str | None = None) -> bytes:
    """The BPF program for ``machine`` (default: this one), as bwrap reads it."""
    arch, nr_socket, nr_io_uring = _ARCHES[(machine or platform.machine()).lower()]
    x86 = arch == 0xC000003E

    # (code, jt, jf, k). Jumps are relative to the next instruction.
    prog: list[tuple[int, int, int, int]] = []

    def add(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> None:
        prog.append((code, jt, jf, k))

    # Labels resolved below: ALLOW is the second to last instruction, DENY the last.
    body: list[tuple[int, int, int, int, str | None, str | None]] = [
        # Another architecture's system calls (32-bit binaries) could use
        # numbers this filter does not check, so they are refused.
        (_LD_W_ABS, 0, 0, _ARCH, None, None),
        (_JEQ_K, 0, 0, arch, None, "DENY"),
        (_LD_W_ABS, 0, 0, _NR, None, None),
    ]
    if x86:  # x32 ABI calls share the x86_64 arch value
        body.append((_JGE_K, 0, 0, _X32_SYSCALL_BIT, "DENY", None))
    body += [
        (_JEQ_K, 0, 0, nr_io_uring, "DENY", None),
        (_JEQ_K, 0, 0, nr_socket, None, "ALLOW"),
        (_LD_W_ABS, 0, 0, _ARG0, None, None),  # domain (low 32 bits)
        (_JEQ_K, 0, 0, _AF_UNIX, "DENY", "ALLOW"),
    ]
    allow_at, deny_at = len(body), len(body) + 1
    for i, (code, jt, jf, k, t_label, f_label) in enumerate(body):
        target = {"ALLOW": allow_at, "DENY": deny_at}
        if t_label:
            jt = target[t_label] - i - 1
        if f_label:
            jf = target[f_label] - i - 1
        add(code, jt, jf, k)
    add(_RET_K, k=_ALLOW)
    add(_RET_K, k=_EPERM)
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog)


@functools.cache
def filter_path() -> str:
    """The filter, written once per process to a private directory.

    The directory is in the temp dir, which sandboxed commands can write, so
    bubblewrap mounts it read-only inside every sandbox (see ``protect``):
    no command can swap in a different filter for the next one.
    """
    directory = tempfile.mkdtemp(prefix="rho-agent-seccomp-")
    atexit.register(shutil.rmtree, directory, True)
    path = os.path.join(directory, "unix-sockets.bpf")
    with open(path, "wb") as f:
        f.write(build_filter())
    os.chmod(path, 0o400)
    return path


def load_args(path: str) -> tuple[list[str], list[str]]:
    """``(prefix, bwrap_options)`` that load the filter at ``path``.

    bwrap reads a filter from an open file descriptor, so ``prefix`` is a small
    shell that opens the file as fd 3 and execs bwrap; callers need no
    ``pass_fds``. The options also mount the filter's directory read-only.
    """
    directory = os.path.dirname(path)
    prefix = ["/bin/sh", "-c", 'exec "$@" 3<"$0"', path]
    return prefix, ["--ro-bind", directory, directory, "--seccomp", "3"]
