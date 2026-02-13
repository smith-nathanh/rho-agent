from __future__ import annotations

import sys

import pytest

import rho_agent.cli as cli


class FakeTyperApp:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def command(self, name: str):
        def decorator(fn):
            return fn

        return decorator

    def __call__(self, *, args: list[str], prog_name: str) -> None:
        self.calls.append((args, prog_name))


def test_cli_defaults_to_tui_when_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = FakeTyperApp()
    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["rho-agent"])

    cli.cli()

    args, prog_name = fake_app.calls[-1]
    assert prog_name == "rho-agent"
    assert args == ["tui"]


def test_cli_routes_unknown_leading_token_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = FakeTyperApp()
    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["rho-agent", "What", "time", "is", "it?"])

    cli.cli()

    args, _prog_name = fake_app.calls[-1]
    assert args == ["main", "What", "time", "is", "it?"]


def test_cli_does_not_prepend_main_for_conduct(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = FakeTyperApp()
    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["rho-agent", "conduct", "--help"])

    cli.cli()

    args, _prog_name = fake_app.calls[-1]
    assert args == ["conduct", "--help"]
