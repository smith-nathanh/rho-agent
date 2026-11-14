"""Tests for building the tool registry from an access level and tool list."""

from __future__ import annotations

import pytest

from rho_agent.sandbox import check_backend
from rho_agent.tools.factory import KNOWN_TOOLS, build_registry

needs_backend = pytest.mark.skipif(check_backend()[0] is None, reason="no sandbox backend")


def test_full_access_has_every_tool_and_no_sandbox(tmp_path):
    reg = build_registry("full", working_dir=str(tmp_path))
    for name in ("read", "grep", "glob", "list", "bash", "view_image", "write", "edit", "delegate"):
        assert name in reg, name
    assert "request_access" not in reg
    assert reg.sandbox is None
    assert reg.get("bash")._sandbox is None


@needs_backend
def test_read_only_has_no_edit_tools(tmp_path):
    reg = build_registry("read-only", working_dir=str(tmp_path))
    assert "write" not in reg
    assert "edit" not in reg
    assert "bash" in reg
    assert "request_access" in reg
    assert reg.sandbox is not None


@needs_backend
def test_workspace_sandboxes_tools(tmp_path):
    reg = build_registry("workspace", working_dir=str(tmp_path))
    assert reg.sandbox is not None
    for name in ("bash", "read", "grep", "glob", "write", "edit"):
        assert reg.get(name)._sandbox is reg.sandbox, name
    assert "request_access" in reg


def test_tool_list_selects_tools(tmp_path):
    reg = build_registry("full", working_dir=str(tmp_path), tools=["bash"])
    assert list(reg._handlers) == ["bash"]


@needs_backend
def test_request_access_kept_with_a_tool_list(tmp_path):
    reg = build_registry("workspace", working_dir=str(tmp_path), tools=["bash"])
    assert sorted(reg._handlers) == ["bash", "request_access"]


def test_unknown_tool_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="Unknown tools: nope"):
        build_registry("full", working_dir=str(tmp_path), tools=["bash", "nope"])
    assert "request_access" in KNOWN_TOOLS


def test_database_readonly_per_connection(tmp_path):
    import sqlite3

    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    databases = {
        "ro": {"type": "sqlite", "path": str(db)},
        "rw": {"type": "sqlite", "path": str(db), "readonly": False},
    }
    handler = build_registry("full", working_dir=str(tmp_path), databases=databases).get("sqlite")
    assert handler._is_readonly("ro") is True
    assert handler._is_readonly("rw") is False
    assert not handler.requires_approval


@needs_backend
def test_read_only_access_forces_read_only_databases(tmp_path):
    import sqlite3

    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    databases = {"rw": {"type": "sqlite", "path": str(db), "readonly": False}}
    reg = build_registry("read-only", working_dir=str(tmp_path), databases=databases)
    assert reg.get("sqlite")._is_readonly("rw") is True
