"""Access: what the agent may change, and how that is enforced.

``Access`` is the setting a user picks (``read-only``, ``workspace`` or
``full``) plus a few details with sensible defaults. Resolving it against a
working directory gives a ``ResolvedPolicy`` with absolute, symlink-free paths.
The OS sandbox enforces that policy for spawned commands; the in-process file
tools check it directly.

``Grant`` is extra access the user allowed during a session (a directory the
agent asked to write, or a protected path it asked to read).
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any


class AccessLevel(Enum):
    """How much the agent may change.

    READ_ONLY: commands can read (except protected paths) and write temp dirs.
    WORKSPACE: also write the working directory, ``add_dirs``, their git
        directories and package-manager caches. The default.
    FULL: no sandbox; commands run with the user's own permissions. For
        disposable machines (containers, CI, remote sandboxes).
    """

    READ_ONLY = "read-only"
    WORKSPACE = "workspace"
    FULL = "full"


# Credentials and keys commands have no business reading.
DEFAULT_DENY_READ: tuple[str, ...] = (
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.kube",
    "~/.docker/config.json",
    "~/.netrc",
    "~/.git-credentials",
)

# Paths that stay read-only inside writable directories. Entries starting with
# ".git/" apply to every git directory in use (including a worktree's main
# repository): a planted hook or a setting such as core.hooksPath would run
# code outside the sandbox the next time the user runs git.
DEFAULT_DENY_WRITE: tuple[str, ...] = (".git/hooks", ".git/config")

# Package-manager caches that installs and builds write to.
DEFAULT_WRITABLE_CACHES: tuple[str, ...] = (
    "~/.cache/uv",
    "~/.cache/pip",
    "~/Library/Caches/pip",
    "~/.cache/pypoetry",
    "~/Library/Caches/pypoetry",
    "~/.npm",
    "~/.cache/yarn",
    "~/Library/Caches/Yarn",
    "~/.local/share/pnpm",
    "~/Library/pnpm",
    "~/.bun/install/cache",
    "~/.cargo/registry",
    "~/.cargo/git",
    "~/go/pkg/mod",
    "~/.cache/go-build",
    "~/Library/Caches/go-build",
    "~/.gradle/caches",
    "~/.m2/repository",
)

# Environment variables removed from sandboxed commands' environment.
DEFAULT_ENV_SCRUB: tuple[str, ...] = (
    "RHO_AGENT_*",
    "*_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)

_DEFAULT_KEYWORD = "default"


def _expand_list(value: Any, defaults: tuple[str, ...], key: str) -> tuple[str, ...]:
    """Parse a list setting. ``"default"`` (alone or as an item) means the defaults;
    ``"none"``, ``false`` or ``[]`` means nothing."""
    if value is None or value == _DEFAULT_KEYWORD or value is True:
        return defaults
    if value in ("none", False):
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"access.{key} must be a list, 'default' or 'none'")
    items: list[str] = []
    for item in value:
        if item == _DEFAULT_KEYWORD:
            items.extend(defaults)
        else:
            items.append(str(item))
    return tuple(dict.fromkeys(items))


def _parse_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("on", "true", "yes", "allow"):
        return True
    if isinstance(value, str) and value.lower() in ("off", "false", "no", "deny"):
        return False
    raise ValueError(f"access.{key} must be on or off, not {value!r}")


@dataclass(frozen=True)
class Access:
    """What the agent may change. ``Access("workspace")`` is the default.

    Args:
        level: ``read-only``, ``workspace`` or ``full``.
        network: Whether commands may use the network.
        unix_sockets: Whether commands may connect to local services over Unix
            sockets (Docker, ssh-agent, tmux, D-Bus, ...). Off by default:
            such a service runs outside the sandbox and would act for them.
        add_dirs: More directories to make writable (``workspace`` only).
        writable_caches, deny_read, deny_write, env_scrub: Details with
            defaults; see the ``DEFAULT_*`` constants.
    """

    level: AccessLevel = AccessLevel.WORKSPACE
    network: bool = True
    unix_sockets: bool = False
    add_dirs: tuple[str, ...] = ()
    writable_caches: tuple[str, ...] = DEFAULT_WRITABLE_CACHES
    deny_read: tuple[str, ...] = DEFAULT_DENY_READ
    deny_write: tuple[str, ...] = DEFAULT_DENY_WRITE
    env_scrub: tuple[str, ...] = DEFAULT_ENV_SCRUB

    def __post_init__(self) -> None:
        # Accept plain strings and lists, as typed in code or read from YAML.
        if not isinstance(self.level, AccessLevel):
            object.__setattr__(self, "level", AccessLevel(self.level))
        for name in ("add_dirs", "writable_caches", "deny_read", "deny_write", "env_scrub"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))

    @property
    def sandboxed(self) -> bool:
        return self.level != AccessLevel.FULL

    @classmethod
    def from_value(cls, value: Any) -> Access:
        """Build from a level name, an ``Access``, or a mapping (as in YAML)."""
        if isinstance(value, Access):
            return value
        if value is None:
            return cls()
        if isinstance(value, (str, AccessLevel)):
            return cls(level=AccessLevel(value))
        if not isinstance(value, Mapping):
            raise ValueError("access must be a level name or a mapping")
        unknown = set(value) - {
            "level",
            "network",
            "unix_sockets",
            "add_dirs",
            "writable_caches",
            "deny_read",
            "deny_write",
            "env_scrub",
        }
        if unknown:
            raise ValueError(f"Unknown access settings: {', '.join(sorted(unknown))}")
        return cls(
            level=AccessLevel(value.get("level", AccessLevel.WORKSPACE.value)),
            network=_parse_bool(value.get("network", True), "network"),
            unix_sockets=_parse_bool(value.get("unix_sockets", False), "unix_sockets"),
            add_dirs=_expand_list(value.get("add_dirs", []), (), "add_dirs"),
            writable_caches=_expand_list(
                value.get("writable_caches"), DEFAULT_WRITABLE_CACHES, "writable_caches"
            ),
            deny_read=_expand_list(value.get("deny_read"), DEFAULT_DENY_READ, "deny_read"),
            deny_write=_expand_list(value.get("deny_write"), DEFAULT_DENY_WRITE, "deny_write"),
            env_scrub=_expand_list(value.get("env_scrub"), DEFAULT_ENV_SCRUB, "env_scrub"),
        )

    def to_value(self) -> str | dict[str, Any]:
        """The shortest form ``from_value`` reads back: the level name when the
        rest is default, else a mapping of the settings that differ."""
        defaults = Access(level=self.level)
        data: dict[str, Any] = {"level": self.level.value}
        for name in (
            "network",
            "unix_sockets",
            "add_dirs",
            "writable_caches",
            "deny_read",
            "deny_write",
        ):
            value = getattr(self, name)
            if value != getattr(defaults, name):
                data[name] = list(value) if isinstance(value, tuple) else value
        if self.env_scrub != defaults.env_scrub:
            data["env_scrub"] = list(self.env_scrub)
        return self.level.value if len(data) == 1 else data

    def with_level(self, level: AccessLevel | str) -> Access:
        return replace(self, level=AccessLevel(level))

    def resolve(self, working_dir: str | Path) -> ResolvedPolicy:
        """Resolve paths against ``working_dir`` and the user's home directory."""
        cwd = _real(working_dir)
        git_denies = tuple(e[len(".git/") :] for e in self.deny_write if _is_git_entry(e))
        writable: list[str] = list(temp_dirs())
        deny_write: list[str] = []
        extra_roots: list[str] = []

        if self.level == AccessLevel.WORKSPACE:
            extra_roots = [_real(_expand(p, cwd)) for p in self.add_dirs]
            other_relative = [
                p for p in self.deny_write if not _is_absolute_ish(p) and not _is_git_entry(p)
            ]
            for root in (cwd, *extra_roots):
                writable.append(root)
                git_dirs = git_dirs_for(root)
                writable.extend(git_dirs)
                deny_write.extend(os.path.join(g, rest) for g in git_dirs for rest in git_denies)
                deny_write.extend(os.path.join(root, entry) for entry in other_relative)
            writable.extend(_real(_expand(p, cwd)) for p in self.writable_caches)

        deny_write.extend(_expand(p, cwd) for p in self.deny_write if _is_absolute_ish(p))

        return ResolvedPolicy(
            level=self.level,
            working_dir=cwd,
            writable=_dedupe(writable),
            deny_write=_dedupe(_real(p) for p in deny_write),
            deny_read=_dedupe(_real(_expand(p, cwd)) for p in self.deny_read),
            network=self.network,
            unix_sockets=self.unix_sockets,
            env_scrub=self.env_scrub,
            extra_roots=_dedupe(extra_roots),
            git_denies=git_denies,
        )


@dataclass(frozen=True)
class Grant:
    """Access the user allowed during a session.

    ``write`` grants make ``path`` writable (and readable, if it was protected).
    Read grants unprotect a ``deny_read`` path.
    """

    path: str
    write: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "write": self.write}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Grant:
        return cls(path=str(data["path"]), write=bool(data.get("write", True)))


@dataclass(frozen=True)
class ResolvedPolicy:
    """An ``Access`` resolved to absolute, symlink-free paths."""

    level: AccessLevel
    working_dir: str
    writable: tuple[str, ...]
    deny_write: tuple[str, ...]
    deny_read: tuple[str, ...]
    network: bool
    unix_sockets: bool = False
    env_scrub: tuple[str, ...] = field(default=DEFAULT_ENV_SCRUB)
    extra_roots: tuple[str, ...] = ()  # add_dirs and write grants, resolved
    git_denies: tuple[str, ...] = ("hooks", "config")  # protected inside git dirs
    grants: tuple[Grant, ...] = ()

    # --- checks ---

    def can_write(self, path: str | Path) -> tuple[bool, str]:
        """Whether a write to ``path`` is allowed. Returns ``(allowed, reason)``."""
        real = _real(path)
        protected = self.protected_for_write(real)
        if protected:
            return False, f"Sandbox: {path} is protected ({protected}) and cannot be written"
        if self.is_writable(real):
            return True, ""
        return False, (
            f"Sandbox ({self.level.value}): {path} is outside the writable directories "
            f"({', '.join(self.writable_summary())})"
        )

    def can_read(self, path: str | Path) -> tuple[bool, str]:
        """Whether reading ``path`` is allowed. Returns ``(allowed, reason)``."""
        denied = self.denied_read(_real(path))
        if denied:
            return False, f"Sandbox: {path} is protected ({_short(denied)}) and cannot be read"
        return True, ""

    def is_writable(self, real: str) -> bool:
        return any(is_within(real, root) for root in self.writable)

    def protected_for_write(self, real: str) -> str | None:
        """The protected path that blocks writing ``real``, if any."""
        for denied in (*self.deny_write, *self.deny_read):
            if is_within(real, denied):
                return _short(denied)
        return None

    def denied_read(self, real: str) -> str | None:
        for denied in self.deny_read:
            if is_within(real, denied):
                return denied
        return None

    # --- grants ---

    def plan_grant(self, path: str | Path, write: bool) -> tuple[Grant | None, str]:
        """The grant that would give access to ``path``, or None and why not.

        A read of a protected path is granted for the whole protected entry
        (``~/.ssh``, not one file in it), so the user sees what they allow.
        """
        real = _real(path)
        denied = self.denied_read(real)
        if not write:
            if denied is None:
                return None, f"{_short(real)} is already readable."
            return Grant(denied, write=False), ""
        for protected in self.deny_write:
            if is_within(real, protected):
                return None, (
                    f"{_short(protected)} is protected and cannot be granted. "
                    "Ask the user to make this change, or to run with --access full."
                )
        if denied is not None:
            return Grant(denied, write=True), ""
        if self.is_writable(real):
            return None, f"{_short(real)} is already writable."
        return Grant(real, write=True), ""

    def with_grants(self, grants: Iterable[Grant]) -> ResolvedPolicy:
        """This policy with ``grants`` applied."""
        grants = tuple(g for g in grants if g not in self.grants)
        if not grants:
            return self
        writable = list(self.writable)
        deny_write = list(self.deny_write)
        deny_read = list(self.deny_read)
        extra = list(self.extra_roots)
        for grant in grants:
            real = _real(grant.path)
            deny_read = [d for d in deny_read if not is_within(d, real)]
            if grant.write:
                git_dirs = git_dirs_for(real)
                writable.extend((real, *git_dirs))
                extra.append(real)
                deny_write.extend(
                    os.path.join(g, rest) for g in git_dirs for rest in self.git_denies
                )
        return replace(
            self,
            writable=_dedupe(writable),
            deny_write=_dedupe(_real(p) for p in deny_write),
            deny_read=_dedupe(deny_read),
            extra_roots=_dedupe(extra),
            grants=(*self.grants, *grants),
        )

    # --- environment and descriptions ---

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for a sandboxed command: ``base`` minus scrubbed variables."""
        env = dict(os.environ if base is None else base)
        for name in list(env):
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.env_scrub):
                del env[name]
        env["RHO_AGENT_SANDBOX"] = self.level.value
        return env

    def writable_summary(self) -> list[str]:
        """Writable places, in words, for messages to the model."""
        if self.level != AccessLevel.WORKSPACE and not self.extra_roots:
            return ["temp directories"]
        roots = [self.working_dir] if self.level == AccessLevel.WORKSPACE else []
        tail = ["temp directories"]
        if self.level == AccessLevel.WORKSPACE:
            tail.append("package-manager caches")
        return [*map(_short, (*roots, *self.extra_roots)), *tail]

    def describe(self) -> str:
        """Plain-language summary for the system prompt and CLI."""
        lines = [f"Access: {self.level.value}. Commands run inside an OS sandbox."]
        lines.append("- Writable: " + ", ".join(self.writable_summary()) + ".")
        lines.append(
            "- Writes anywhere else fail with 'Operation not permitted' or 'Read-only file system'."
        )
        if self.deny_write:
            lines.append(
                "- Protected even inside writable directories: "
                + ", ".join(map(_short, self.deny_write))
            )
        if self.deny_read:
            lines.append("- Unreadable: " + ", ".join(map(_short, self.deny_read)))
        lines.append(
            "- Network access: " + ("allowed." if self.network else "blocked (no internet).")
        )
        if not self.unix_sockets:
            lines.append(
                "- Local services over Unix sockets (Docker, ssh-agent, tmux, ...): blocked."
            )
        return "\n".join(lines)


def temp_dirs() -> tuple[str, ...]:
    """Temp directories sandboxed commands may always write."""
    dirs = [tempfile.gettempdir(), "/tmp"]
    if os.environ.get("TMPDIR"):
        dirs.append(os.environ["TMPDIR"])
    return _dedupe(_real(d) for d in dirs if os.path.isdir(d))


def git_dirs_for(root: str) -> tuple[str, ...]:
    """Git directories that commits made from ``root`` write to.

    For an ordinary repository that is its ``.git`` directory. For a worktree
    (``.git`` is a file) it is the worktree's own git dir plus the main
    repository's common dir, where objects and refs live. ``root`` may be a
    subdirectory of the repository.
    """
    current = Path(root)
    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            git_dir = dot_git
            break
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return ()
            if not text.startswith("gitdir:"):
                return ()
            git_dir = Path(text[len("gitdir:") :].strip())
            if not git_dir.is_absolute():
                git_dir = candidate / git_dir
            break
    else:
        return ()

    dirs = [_real(git_dir)]
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            common = Path(commondir_file.read_text(encoding="utf-8").strip())
        except OSError:
            common = None
        if common is not None:
            if not common.is_absolute():
                common = git_dir / common
            dirs.append(_real(common))
    return _dedupe(dirs)


def is_within(path: str, root: str) -> bool:
    """True if ``path`` is ``root`` or inside it (both absolute and resolved)."""
    if root == os.sep:
        return True
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _short(p: str) -> str:
    """``p`` with the home directory written as ``~``."""
    home = str(Path.home())
    return ("~" + p[len(home) :]) if p.startswith(home + os.sep) else p


def _is_git_entry(p: str) -> bool:
    """``.git`` or a path inside it, which applies to every git directory in use."""
    return p == ".git" or p.startswith(".git/")


def _is_absolute_ish(p: str) -> bool:
    return p.startswith(("/", "~"))


def _expand(p: str, cwd: str) -> str:
    expanded = os.path.expanduser(p)
    return expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
