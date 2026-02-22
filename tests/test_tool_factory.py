"""Tests for ToolFactory registry construction from profiles."""

from __future__ import annotations

from unittest.mock import patch

from rho_agent.permissions import PermissionProfile
from rho_agent.permissions.factory import ToolFactory


def _build_registry(profile: PermissionProfile):
    """Build a registry with DB registration patched out."""
    with patch.object(ToolFactory, "_register_database_tools"):
        factory = ToolFactory(profile)
        return factory.create_registry()


def test_readonly_has_bash_restricted():
    reg = _build_registry(PermissionProfile.readonly())
    handler = reg.get("bash")
    assert handler is not None
    assert handler._restricted is True


def test_readonly_no_write_tools():
    reg = _build_registry(PermissionProfile.readonly())
    assert reg.get("write") is None
    assert reg.get("edit") is None


def test_developer_has_write_and_edit():
    reg = _build_registry(PermissionProfile.developer())
    assert reg.get("write") is not None
    assert reg.get("edit") is not None


def test_developer_bash_unrestricted():
    reg = _build_registry(PermissionProfile.developer())
    handler = reg.get("bash")
    assert handler is not None
    assert handler._restricted is False


def test_bash_only_mode_minimal():
    profile = PermissionProfile(name="minimal", bash_only=True)
    reg = _build_registry(profile)
    assert reg.get("bash") is not None
    assert reg.get("read") is None
    assert reg.get("grep") is None
    assert reg.get("glob") is None


def test_core_tools_always_present():
    reg = _build_registry(PermissionProfile.readonly())
    for name in ("read", "grep", "glob", "list"):
        assert reg.get(name) is not None, f"Expected '{name}' in registry"
