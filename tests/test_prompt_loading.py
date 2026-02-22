"""Tests for prompt loading and rendering."""

from __future__ import annotations

import pytest

from rho_agent.prompts.loader import Prompt, PromptVariable, parse_frontmatter
from rho_agent.prompts.renderer import prepare_prompt, render_string


# --- parse_frontmatter ---

def test_parse_frontmatter_extracts_yaml_and_body():
    content = "---\ntitle: Test\nauthor: Nobody\n---\nHello, world!"
    fm, body = parse_frontmatter(content)
    assert fm["title"] == "Test"
    assert fm["author"] == "Nobody"
    assert body == "Hello, world!"


def test_parse_frontmatter_no_frontmatter():
    content = "Just plain text\nwith no YAML."
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content.strip()


def test_parse_frontmatter_unclosed_returns_raw():
    content = "---\ntitle: Test\nNo closing delimiter here."
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == content.strip()


# --- prepare_prompt ---

def test_prepare_prompt_renders_variables():
    prompt = Prompt(
        description="test",
        variables=[PromptVariable(name="name")],
        system_prompt="Hello, {{ name }}!",
    )
    system, _ = prepare_prompt(prompt, {"name": "Alice"})
    assert system == "Hello, Alice!"


def test_prepare_prompt_uses_defaults():
    prompt = Prompt(
        description="test",
        variables=[PromptVariable(name="name", default="World")],
        system_prompt="Hello, {{ name }}!",
    )
    system, _ = prepare_prompt(prompt, {})
    assert system == "Hello, World!"


def test_prepare_prompt_missing_required_raises():
    prompt = Prompt(
        description="test",
        variables=[PromptVariable(name="name", required=True)],
        system_prompt="Hello, {{ name }}!",
    )
    with pytest.raises(ValueError, match="required"):
        prepare_prompt(prompt, {})


# --- render_string ---

def test_render_string_undefined_variable_renders_empty():
    """Jinja2 default Undefined is permissive — undefined vars render as empty."""
    result = render_string("Hello, {{ missing }}!", {})
    assert "missing" not in result
