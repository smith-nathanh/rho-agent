# Sandbox: known issues

Working notes on what the OS sandbox does not cover yet, what was tried, and
what a fix would look like. User-facing docs are in [site/access.md](site/access.md);
the code is in `rho_agent/sandbox/`.

What the sandbox is for: stop an agent from changing things outside the
project by mistake, and make a deliberate escape (an agent following injected
instructions) hard. The first holds today. The open issues below are all of the
second kind: a command that *tries* to get out.

Last reviewed: 2026-11-14.

## Open

### 1. macOS: other apps can act for a sandboxed command (Mach IPC)

**What:** the Seatbelt profile starts from `(allow default)` and denies file
writes, protected reads, Unix sockets and (optionally) IP networking. Mach IPC
is not restricted, so a command can ask an unsandboxed process to do work:

```bash
rho-agent sandbox run -- open -a Calculator                    # launches the app
rho-agent sandbox run -- osascript -e 'tell application "Terminal" to do script "touch ~/escaped"'
```

The launched app or the Terminal command runs with the user's full rights.
`osascript` driving another app may hit a macOS Automation (TCC) prompt the
first time, but `open` does not.

**Tried (phase 1):**
- `(deny appleevent-send)` and denying `mach-lookup` of individual
  LaunchServices services: did not stop `open` or `osascript`.
- Denying all `mach-lookup`: stops them, and python/git/uv still ran, but DNS
  (mDNSResponder), TLS certificate checks (trustd) and keychain access break.

**Fix:** a deny-by-default profile, `(deny default)`, that allows file reads,
process exec/fork, sysctl reads, and an explicit list of Mach services
(DNS, trustd, keychain, notification center, ...), as Codex and Anthropic's
sandbox-runtime do. Needs testing against git, Python, Node, uv, curl,
Homebrew-installed tools and HTTPS. Keep `allow default` as a fallback setting
in case the list misses something.

### 2. Local TCP services are reachable when network is on

**What:** with `network: true` (the default) commands can connect to anything,
including services on `localhost`: Docker if it listens on TCP (2375/2376),
databases, Jupyter, Ollama, a browser's remote-debugging port (full control of
the browser), dev servers with admin endpoints. Like Unix sockets (now
blocked), these run outside the sandbox.

**Fix options:**
- macOS: `(deny network-outbound (remote ip "localhost:*"))`, with a setting
  to allow listed ports. Needs checking that tools using loopback internally
  (some test runners, language servers) still work.
- Linux: loopback is shared unless the network namespace is unshared, which
  also cuts the internet. A proxy (outside the namespace, like Codex and
  sandbox-runtime) would give both a loopback block and a domain allowlist; see 3.

### 3. Network is all-or-nothing

**What:** no per-domain allowlist. With network on, a command can read
anything not in `deny_read` (see 4) and send it anywhere.

**Fix:** run an HTTP/SOCKS proxy in the agent process, force sandboxed
commands through it (macOS: allow only the proxy's localhost port; Linux:
unshared network plus a socket bridged into the namespace), and filter by
domain. This is how Codex and sandbox-runtime do it. A `network: [domains]`
setting would sit next to `true`/`false`.

### 4. Reads are a denylist

**What:** commands can read everything except `deny_read` (`~/.ssh`, `~/.aws`,
`~/.gnupg`, `~/.config/gh`, `~/.netrc`, ...). Secrets elsewhere are readable:
`.env` files in other projects, `~/.secure_files/`, browser profiles, shell
history, `~/.config/*` of other tools. Combined with 3, that is an exfiltration
path.

**Workaround:** add paths: `access: {deny_read: [default, ~/.secure_files]}`.

**Fix options:** widen the default list (shell history, browser profiles,
`~/Library/Keychains`, `~/.config/*/credentials*`); or offer a stricter mode that
denies the home directory except the working directory, caches and tool
configs, as Claude Code's sandbox can.

### 5. Environment scrubbing is pattern-based

**What:** commands lose `RHO_AGENT_*`, `*_API_KEY`, `AWS_SECRET_ACCESS_KEY` and
`AWS_SESSION_TOKEN`. Other secrets pass through: `GITHUB_TOKEN`, `HF_TOKEN`,
`*_SECRET`, `PGPASSWORD`, `DATABASE_URL`, ...

**Fix:** add `*_TOKEN`, `*_SECRET`, `*_SECRET_*`, `*PASSWORD*`, `DATABASE_URL`
to `DEFAULT_ENV_SCRUB` (`sandbox/policy.py`), then check that common tools
(gh, git, cloud CLIs) degrade sensibly rather than break.

### 6. Linux: a protected path that doesn't exist can't be guarded

**What:** bubblewrap protects paths by mounting over them, which needs them to
exist. In a repository without `.git/hooks`, a command can create
`.git/hooks/pre-commit`, and it runs outside the sandbox the next time the
user commits. (Seatbelt rules match by path, so macOS is not affected.)

**Fix:** create missing protected directories (empty) before spawning, or
mount an empty read-only directory there with `--dir` plus `--remount-ro`.

### 7. Files the agent writes are trusted later

**What:** inherent to letting the agent edit the project. Files that other
tools run automatically are code that will execute outside the sandbox:
`.envrc` (direnv), `.vscode/tasks.json` and settings, `Makefile`,
`package.json` scripts, `.pre-commit-config.yaml`, `pyproject.toml` build hooks.
`.git/hooks` and `.git/config` are protected because git runs them silently;
these mostly need the user to run something.

**Fix:** none planned. Possibly add `.envrc` and `.vscode/` to the default
`deny_write`. Review diffs to these before running them.

### 8. Linux: Unix sockets are blocked entirely

**What:** seccomp sees system call arguments, not paths, so on Linux
`unix_sockets: false` blocks creating any `AF_UNIX` socket, including ones
the agent's own work needs: a PostgreSQL started in the project, `git
credential-cache`, `docker`, `ssh-agent`, Python `multiprocessing.Manager`.
macOS blocks only connections, so a server and client inside the sandbox
still can't talk there either (connect is refused), but socket creation works.
`socketpair()` works on both (asyncio, multiprocessing pools).

**Workarounds:** the agent can ask to run one command with `unsandboxed: true`,
or set `unix_sockets: true`.

**Fix options:** allow sockets under the writable directories on macOS
(`(allow network-outbound (remote unix-socket (subpath ...)))`). On Linux,
a seccomp user-notification supervisor could check the path, at a cost in
complexity.

### 9. Linux: seccomp filter limits

- Built for x86_64 and aarch64 only. Other architectures fail closed with an
  error suggesting `unix_sockets: true`.
- System calls from another architecture (32-bit binaries on x86_64) are
  refused outright, so 32-bit programs can't run in the sandbox.
- `io_uring_setup` is refused (io_uring can create sockets without the
  `socket` system call). Tools using io_uring fall back or fail.

### 10. In-process tools enforce the policy in Python

**What:** `read`, `write`, `edit`, `list`, `view_image` and `read_excel` run in
the agent process, which is not sandboxed. They resolve symlinks and check the
same policy, but a symlink swapped between the check and the open (a race a
concurrent background command could set up) could get past it.

**Fix:** open with `O_NOFOLLOW` along the path, or run file tools through the
sandbox too.

### 11. Platform coverage

- **Windows:** no backend. Options: WSL2, or `full` access in a container.
  A native one would need AppContainer or a restricted token plus ACLs.
- **Inside containers:** bubblewrap usually can't create namespaces; use
  `full` (the container is the boundary).
- **Ubuntu 24.04+:** AppArmor restricts unprivileged user namespaces unless
  `kernel.apparmor_restrict_unprivileged_userns=0`. CI sets it; not yet
  checked on a desktop install.
- **bubblewrap older than 0.5:** `deny_read` directories show as empty instead
  of refusing access (no `--perms`).
- **`sandbox-exec`** is deprecated by Apple but still shipped and used by
  Codex, Claude Code and Chrome. If it goes away, macOS needs another backend.

## Verified

- macOS 26 (arm64): full test suite, `rho-agent sandbox check`, and a live
  model run (rename across a granted sibling directory; a home-directory
  request declined).
- Linux, Debian 12 container (aarch64), bubblewrap 0.8, as an unprivileged
  user: full test suite, `sandbox check`, the same live model run, and with
  Unix sockets blocked: curl, DNS, `git clone`, `uv pip install`, asyncio,
  multiprocessing pools.
- Not yet: x86_64 Linux outside CI, an Ubuntu 24.04 desktop, Fedora/Arch.

## Fixed

- **Unix sockets (2026-11-14).** Commands could connect to Docker
  (`docker run -v ~:/host ...` is a full escape), ssh-agent, tmux, D-Bus.
  macOS: Seatbelt denies `network-outbound` to Unix sockets except
  `/private/var/run/mDNSResponder` (DNS) and `/private/var/run/syslog`.
  Linux: a seccomp filter (`sandbox/seccomp.py`) refuses `socket(AF_UNIX)` and
  `io_uring_setup`. The filter file sits in a private temp directory that
  every sandbox mounts read-only, so a command can't replace it for the next
  one. Setting: `unix_sockets: true` turns it off.
- **Linux `deny_read` directories looked empty (2026-11-14).** bubblewrap
  hid them with an empty tmpfs, so the agent saw an empty `~/.ssh` instead of
  a refusal. Now mounted with mode 000 ("Permission denied").
