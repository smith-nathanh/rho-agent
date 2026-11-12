"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_project_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this repo's AGENTS.md and the developer's own skills out of tests.

    Tests that cover project instructions or skills turn them back on.
    """
    monkeypatch.setenv("RHO_AGENT_PROJECT_INSTRUCTIONS", "0")
    monkeypatch.setenv("RHO_AGENT_SKILLS", "0")
