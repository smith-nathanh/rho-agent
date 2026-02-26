"""Git helpers using asyncio subprocess."""

from __future__ import annotations

import asyncio


async def _run_git(*args: str, working_dir: str, check: bool = True) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=working_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return proc.returncode, stdout.decode(), stderr.decode()


async def get_head_sha(working_dir: str) -> str:
    """Return the current HEAD SHA."""
    _, stdout, _ = await _run_git("rev-parse", "HEAD", working_dir=working_dir)
    return stdout.strip()


async def git_add_and_commit(working_dir: str, message: str) -> str | None:
    """Stage all changes and commit. Returns SHA or None if nothing to commit."""
    await _run_git("add", "-A", working_dir=working_dir)
    rc, _, _ = await _run_git("diff", "--cached", "--quiet", working_dir=working_dir, check=False)
    if rc == 0:
        return None  # nothing staged
    await _run_git("commit", "-m", message, working_dir=working_dir)
    return await get_head_sha(working_dir)


async def git_diff_since(working_dir: str, base_sha: str) -> str:
    """Return the diff from base_sha to HEAD."""
    _, stdout, _ = await _run_git("diff", base_sha, "HEAD", working_dir=working_dir)
    return stdout


async def create_branch(working_dir: str, name: str) -> None:
    """Create and checkout a new branch."""
    await _run_git("checkout", "-b", name, working_dir=working_dir)


async def is_worktree_clean(working_dir: str) -> bool:
    """Return True if there are no staged/unstaged/untracked changes."""
    _, stdout, _ = await _run_git(
        "status",
        "--porcelain",
        working_dir=working_dir,
        check=False,
    )
    return not stdout.strip()
