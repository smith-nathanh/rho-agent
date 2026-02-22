"""Tests for output truncation."""

from __future__ import annotations

from rho_agent.core.truncate import truncate_output


def test_short_content_unchanged():
    content = "Hello, world!"
    assert truncate_output(content) == content


def test_long_content_truncated_with_marker():
    content = "x" * 100_000
    result = truncate_output(content, max_tokens=100)
    assert "truncated" in result


def test_truncated_has_head_and_tail():
    content = "AAAA" + ("x" * 100_000) + "ZZZZ"
    result = truncate_output(content, max_tokens=100)
    assert result.startswith("Total output lines:")
    assert "AAAA" in result
    assert "ZZZZ" in result


def test_truncated_has_line_count_header():
    content = "line1\nline2\n" * 10_000
    result = truncate_output(content, max_tokens=100)
    assert result.startswith("Total output lines:")
