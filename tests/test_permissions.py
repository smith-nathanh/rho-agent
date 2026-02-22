"""Tests for permission profiles and approval logic."""

from __future__ import annotations

from rho_agent.permissions import (
    ApprovalMode,
    DatabaseMode,
    FileWriteMode,
    PermissionProfile,
    ShellMode,
)


def test_readonly_profile_modes():
    p = PermissionProfile.readonly()
    assert p.shell == ShellMode.RESTRICTED
    assert p.file_write == FileWriteMode.OFF
    assert p.database == DatabaseMode.READONLY


def test_developer_profile_modes():
    p = PermissionProfile.developer()
    assert p.shell == ShellMode.UNRESTRICTED
    assert p.file_write == FileWriteMode.FULL


def test_eval_profile_modes():
    p = PermissionProfile.eval()
    assert p.shell == ShellMode.UNRESTRICTED
    assert p.database == DatabaseMode.MUTATIONS
    assert p.approval == ApprovalMode.NONE


def test_requires_approval_none_mode():
    p = PermissionProfile.eval()
    assert p.requires_tool_approval("bash") is False
    assert p.requires_tool_approval("write") is False
    assert p.requires_tool_approval("read") is False


def test_requires_approval_all_mode():
    p = PermissionProfile(name="strict", approval=ApprovalMode.ALL)
    assert p.requires_tool_approval("bash") is True
    assert p.requires_tool_approval("read") is True
    assert p.requires_tool_approval("anything") is True


def test_requires_approval_dangerous_mode():
    p = PermissionProfile.readonly()
    assert p.requires_tool_approval("bash") is True
    assert p.requires_tool_approval("read") is False
    assert p.requires_tool_approval("grep") is False


def test_from_dict_yaml_false_as_off():
    """YAML parses bare 'off' as boolean False; from_dict should handle it."""
    profile = PermissionProfile.from_dict({"file_write": False})
    assert profile.file_write == FileWriteMode.OFF
