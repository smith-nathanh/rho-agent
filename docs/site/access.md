---
title: Access
description: What an agent can change, the OS sandbox that enforces it, and how agents ask for more.
order: 9
---

One setting decides what an agent can change. Commands run inside the operating system's sandbox, so the limits hold whatever the command is. When the agent needs more, it asks you.

## Levels

| Access | The agent can write | Sandbox | For |
|---|---|---|---|
| `workspace` (default) | The working directory, `--add-dir` directories, temp directories and package-manager caches | Yes | Everyday work in a project |
| `read-only` | Temp directories only. The `write` and `edit` tools are not offered | Yes | Investigating without changing anything |
| `full` | Anywhere you can | No | Containers, CI, remote sandboxes: machines you can throw away |

```bash
rho-agent                          # workspace
rho-agent --access read-only
rho-agent --access full            # inside a container
rho-agent --add-dir ../shared-lib  # workspace, plus write access to ../shared-lib
```

```python
AgentConfig(access="read-only")
AgentConfig(access=Access("workspace", network=False, add_dirs=["~/proj/shared-lib"]))
```

`RHO_AGENT_ACCESS` sets the default level for both.

In `read-only` and `workspace`:

- Commands can read everything except credentials: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.config/gcloud`, `~/.config/gh`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`, `~/.git-credentials`. Add your own with `deny_read` (see [Settings](#settings)).
- `.git/hooks` and `.git/config` stay read-only even inside the project. A planted hook or setting would otherwise run outside the sandbox the next time you use git. Commits work, including from a git worktree.
- Commands can't connect to local services over Unix sockets: Docker, ssh-agent, tmux, D-Bus and the like. Those services run outside the sandbox, so reaching one would be a way out (`docker run -v ~:/host ...` can change anything). DNS lookups still work.
- API keys (`*_API_KEY`, `RHO_AGENT_*`, AWS secrets) are removed from commands' environment.
- Network access is on. Turn it off with `network: false`.

The agent is told its limits in the default system prompt. When a command fails because of the sandbox, a note saying so is added to its output.

## Asking for more

When the agent needs to write outside its sandbox, it asks for the directory:

```
╭─ agent wants write access outside the sandbox ─────────────╮
│ /Users/you/proj/shared-lib                                 │
│ Need to fix the import in shared-lib/utils.py              │
╰────────────────────────────────────────────────────────────╯
Allow for this session? [y/N]:
```

- A yes lasts for the rest of the session, including when you resume it. It covers every tool and every command. A no is final for that request, and the agent is told to find another way.
- `write` and `edit` ask on their own when given a path outside. In shell commands, the agent calls the `request_access` tool after a command is refused, or before work it knows will need it.
- The agent can ask to *read* a protected path the same way, for example `~/.ssh` before a `git push`. It is shown the whole protected entry, not one file in it.
- Protected paths inside the project (`.git/hooks`, `.git/config`) cannot be granted.
- For a command that can't work in a sandbox at all, such as a system package install or `docker build`, the agent can ask to run that one command unsandboxed: *"agent wants to run a command outside the sandbox … Run it? [y/N]"*. Each such command is asked about separately.

Who answers:

| Where the agent runs | Requests go to |
|---|---|
| Interactive terminal | The prompt, as above |
| No terminal, or `--queue-approvals` | [`rho-agent monitor`](monitor/), with `approve <id>` or `deny <id>` |
| Python, with `session.approval_callback` set | Your callback: `async (tool_name, tool_args) -> bool` |
| Python, with no callback | Nobody. The agent is not offered `request_access` and stays inside the sandbox |

In a session, `/access` shows what the agent can reach and what was granted. `/access revoke <path>` takes a grant back, and `/access read-only` (or `workspace`, `full`) changes the level.

## Settings

The level and `add_dirs` cover most needs. The rest have defaults, and can be set in an [agent config file](python-sdk/) or with `Access(...)`:

```yaml
access:
  level: workspace
  network: false
  unix_sockets: false          # true lets commands reach Docker, ssh-agent, ...
  add_dirs: [~/proj/shared-lib]
  # A list replaces the default; the item "default" includes it; "none" means nothing.
  deny_read: [default, ~/secrets]
  deny_write: default          # .git/hooks and .git/config; relative entries apply in each writable dir
  writable_caches: default     # uv, pip, poetry, npm, yarn, pnpm, bun, cargo, go, gradle, maven
  env_scrub: default
```

`access: read-only` on its own also works.

**Databases** are read-only per connection. Set `readonly: false` on a connection in `databases.yaml` to allow writes; `read-only` access keeps every connection read-only regardless.

**Tools**: `AgentConfig(tools=["bash"])` gives the agent only the tools named. `request_access` is added whenever there is a sandbox.

## How it works

The agent process itself is not sandboxed: it needs the network for the model API and writes its own session files. What runs sandboxed is every command it starts: `bash` (including background commands) and the `rg` processes behind `grep` and `glob`. Each command is wrapped in the platform's sandbox with the session's current policy, so a grant applies from the next command on, and everything a command starts inherits its limits.

The file tools (`read`, `write`, `edit`, `list`, `view_image`, `read_excel`) run inside the agent process and check the same policy themselves before touching a path, after resolving symlinks.

**macOS** uses Seatbelt, through `/usr/bin/sandbox-exec`, with a profile generated per policy. It allows everything, then denies:

- writes outside the writable directories, and to protected paths inside them;
- reads of `deny_read` paths;
- connections to Unix sockets, except the system's DNS and logging sockets;
- IP networking, when `network` is off.

**Linux** uses [bubblewrap](https://github.com/containers/bubblewrap), which builds a private view of the filesystem for the command:

- `/` is mounted read-only, the writable directories are mounted writable on top, and protected paths are mounted read-only again;
- `deny_read` directories are covered by an empty directory that can't be opened, and files by an unreadable device;
- a seccomp filter makes creating a Unix socket fail. Socket pairs between the command's own processes still work, which asyncio and multiprocessing need;
- with `network` off, the command gets its own network namespace with no interfaces;
- the command gets its own process namespace, so stopping it stops everything it started.

Sandboxed commands see `RHO_AGENT_SANDBOX=<level>` in their environment.

If `read-only` or `workspace` is asked for and the sandbox can't run, rho-agent stops with an error rather than run unsandboxed.

## Platforms

| Platform | Sandbox | Setup |
|---|---|---|
| macOS | Seatbelt (`sandbox-exec`) | Built in |
| Linux (x86_64, arm64) | [bubblewrap](https://github.com/containers/bubblewrap) | `sudo apt install bubblewrap` (or `dnf`, `pacman`). On Ubuntu 24.04+, unprivileged user namespaces may need enabling: `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0` |
| Windows | None | Use WSL2, or `full` access in a container |

`rho-agent sandbox check` shows what is detected and runs a self-test: writes inside and outside the project, git hooks, credentials, API keys, network and Unix sockets. `rho-agent sandbox run -- <command>` runs one command sandboxed. Inside containers, use `full`: the container is the boundary, and bubblewrap usually cannot create namespaces there.

Remote backends ([Daytona](daytona/)) run with `full` access inside their VM.

## Known limits

The sandbox reliably stops an agent changing things outside the project by mistake. These are ways a command that *tries* to get out still could:

- **macOS: other apps.** Commands can still ask running apps to act for them over Mach IPC, for example `open` launching an app or `osascript` telling Terminal to run something.
- **Local network services.** With network on, commands can reach services on `localhost`, such as databases, Jupyter or Docker listening on TCP.
- **Network is all-or-nothing.** There is no per-domain allowlist yet.
- **Reads are a denylist.** Secrets outside the default `deny_read` paths, like `.env` files in other projects, are readable. Add them to `deny_read`.
- **Environment scrubbing is by pattern.** Tokens not matching `*_API_KEY` (`GITHUB_TOKEN`, `PGPASSWORD`, ...) reach commands.
- **Linux:** a protected path that doesn't exist yet can't be guarded, for example a repository without a `.git/hooks` directory. Blocking Unix sockets also blocks ones the project itself needs, such as a local PostgreSQL; the agent can ask to run such a command unsandboxed, or set `unix_sockets: true`.

Details, what was tried and the planned fixes are in [`docs/sandboxing-known-issues.md`](https://github.com/smith-nathanh/rho-agent/blob/main/docs/sandboxing-known-issues.md).
