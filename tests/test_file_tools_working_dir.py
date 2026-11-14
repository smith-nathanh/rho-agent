"""File tools resolve relative paths against the agent's working directory."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.factory import build_registry
from rho_agent.tools.handlers.paths import resolve_path

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A working directory for the agent and a different directory for the process."""
    work = tmp_path / "work"
    elsewhere = tmp_path / "elsewhere"
    work.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return work, elsewhere


async def _call(registry, tool: str, **arguments):
    handler = registry.get(tool)
    assert handler is not None
    return await handler.handle(ToolInvocation(call_id="t", tool_name=tool, arguments=arguments))


def _registry(working_dir: Path | None):
    # Full access: these tests are about path resolution, not the sandbox.
    return build_registry("full", working_dir=str(working_dir) if working_dir else None)


async def test_write_relative_path_lands_in_working_dir(dirs):
    work, elsewhere = dirs
    out = await _call(_registry(work), "write", path="sub/new.txt", content="hi\n")
    assert out.success, out.content
    assert (work / "sub" / "new.txt").read_text() == "hi\n"
    assert not (elsewhere / "sub").exists()


async def test_edit_relative_path_edits_file_in_working_dir(dirs):
    work, elsewhere = dirs
    (work / "f.py").write_text("x = 1\n")
    (elsewhere / "f.py").write_text("x = 1\n")
    out = await _call(_registry(work), "edit", path="f.py", old_string="x = 1", new_string="x = 2")
    assert out.success, out.content
    assert (work / "f.py").read_text() == "x = 2\n"
    assert (elsewhere / "f.py").read_text() == "x = 1\n"


async def test_read_relative_path_reads_from_working_dir(dirs):
    work, elsewhere = dirs
    (work / "f.txt").write_text("from work\n")
    (elsewhere / "f.txt").write_text("from elsewhere\n")
    out = await _call(_registry(work), "read", path="f.txt")
    assert out.success, out.content
    assert "from work" in out.content
    assert "from elsewhere" not in out.content


async def test_list_relative_path_lists_working_dir(dirs):
    work, elsewhere = dirs
    (work / "in_work.txt").write_text("")
    (elsewhere / "in_elsewhere.txt").write_text("")
    out = await _call(_registry(work), "list", path=".")
    assert out.success, out.content
    assert "in_work.txt" in out.content
    assert "in_elsewhere.txt" not in out.content


@needs_rg
async def test_glob_relative_path_searches_working_dir(dirs):
    work, elsewhere = dirs
    (work / "in_work.py").write_text("")
    (elsewhere / "in_elsewhere.py").write_text("")
    out = await _call(_registry(work), "glob", pattern="*.py", path=".")
    assert out.success, out.content
    assert "in_work.py" in out.content
    assert "in_elsewhere.py" not in out.content


@needs_rg
async def test_grep_relative_path_searches_working_dir(dirs):
    work, elsewhere = dirs
    (work / "a.txt").write_text("needle work\n")
    (elsewhere / "a.txt").write_text("needle elsewhere\n")
    out = await _call(_registry(work), "grep", pattern="needle", path=".")
    assert out.success, out.content
    assert "needle work" in out.content
    assert "needle elsewhere" not in out.content


async def test_view_image_relative_path_uses_working_dir(dirs):
    work, _ = dirs
    # Smallest thing the handler accepts as a PNG: it only needs to find the file.
    (work / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    out = await _call(_registry(work), "view_image", path="shot.png")
    assert "not found" not in out.content.lower()


async def test_absolute_and_home_paths_ignore_working_dir(dirs, monkeypatch):
    work, elsewhere = dirs
    target = elsewhere / "abs.txt"
    out = await _call(_registry(work), "write", path=str(target), content="abs\n")
    assert out.success, out.content
    assert target.read_text() == "abs\n"

    monkeypatch.setenv("HOME", str(elsewhere))
    assert resolve_path("~/x.txt", work) == (elsewhere / "x.txt").resolve()


async def test_no_working_dir_uses_process_directory(dirs):
    _, elsewhere = dirs
    out = await _call(_registry(None), "write", path="cwd.txt", content="cwd\n")
    assert out.success, out.content
    assert (elsewhere / "cwd.txt").read_text() == "cwd\n"
