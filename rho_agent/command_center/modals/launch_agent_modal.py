from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.validation import ValidationResult, Validator
from textual.widgets import Button, Checkbox, Input, Label, Static


class ExistingDirectory(Validator):
    """Validate that an input points to an existing directory."""

    def validate(self, value: str) -> ValidationResult:
        p = Path(value).expanduser() if value else Path()
        ok = value.strip() != "" and p.exists() and p.is_dir()
        return self.success() if ok else self.failure("Working dir must exist")


@dataclass(slots=True)
class LaunchModalResult:
    working_dir: Path
    profile: str
    model: str
    prompt: str
    auto_approve: bool
    team_id: str
    project_id: str


class LaunchAgentModal(ModalScreen[LaunchModalResult | None]):
    """Modal dialog to launch a new agent."""

    DEFAULT_CSS = """
    LaunchAgentModal {
        align: center middle;
    }

    #dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        border: round $surface;
        padding: 1 2;
        background: $panel;
    }

    #error {
        color: $error;
        height: auto;
        padding: 0 0 1 0;
    }

    .row {
        height: auto;
        margin: 0 0 1 0;
    }

    .label {
        width: 18;
    }

    Input {
        width: 1fr;
    }

    #buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        working_dir: str = ".",
        profile: str = "readonly",
        model: str = "gpt-5-mini",
        prompt: str = "",
        auto_approve: bool = False,
        team_id: str = "",
        project_id: str = "",
    ) -> None:
        super().__init__()
        self._defaults = {
            "working_dir": working_dir,
            "profile": profile,
            "model": model,
            "prompt": prompt,
            "auto_approve": auto_approve,
            "team_id": team_id,
            "project_id": project_id,
        }

    class Submitted(Message):
        def __init__(self, result: LaunchModalResult) -> None:
            self.result = result
            super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Launch agent", id="title")
            yield Label("", id="error")

            with Horizontal(classes="row"):
                yield Static("Working dir", classes="label")
                yield Input(
                    value=self._defaults["working_dir"],
                    id="working-dir",
                    validators=[ExistingDirectory()],
                )

            with Horizontal(classes="row"):
                yield Static("Profile", classes="label")
                yield Input(value=self._defaults["profile"], id="profile")

            with Horizontal(classes="row"):
                yield Static("Model", classes="label")
                yield Input(value=self._defaults["model"], id="model")

            with Horizontal(classes="row"):
                yield Static("Prompt", classes="label")
                yield Input(value=self._defaults["prompt"], id="prompt")

            with Horizontal(classes="row"):
                yield Static("Team ID", classes="label")
                yield Input(value=self._defaults["team_id"], id="team-id")

            with Horizontal(classes="row"):
                yield Static("Project ID", classes="label")
                yield Input(value=self._defaults["project_id"], id="project-id")

            with Horizontal(classes="row"):
                yield Static("Auto approve", classes="label")
                yield Checkbox(value=bool(self._defaults["auto_approve"]), id="auto-approve")

            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Launch", id="launch", variant="primary")

    def _set_error(self, text: str) -> None:
        self.query_one("#error", Label).update(text)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "launch":
            return

        working_dir_raw = self.query_one("#working-dir", Input).value.strip()
        profile = self.query_one("#profile", Input).value.strip() or "readonly"
        model = self.query_one("#model", Input).value.strip() or "gpt-5-mini"
        prompt = self.query_one("#prompt", Input).value.strip()
        auto_approve = self.query_one("#auto-approve", Checkbox).value
        team_id = self.query_one("#team-id", Input).value.strip()
        project_id = self.query_one("#project-id", Input).value.strip()

        # Validate working dir via Input validators.
        wd_input = self.query_one("#working-dir", Input)
        validation = wd_input.validate(working_dir_raw)
        if not validation.is_valid:
            self._set_error("Working dir must exist")
            return

        if not profile:
            self._set_error("Profile is required")
            return
        if not model:
            self._set_error("Model is required")
            return
        if bool(team_id) != bool(project_id):
            self._set_error("Provide both Team ID and Project ID, or leave both blank")
            return

        self._set_error("")
        self.dismiss(
            LaunchModalResult(
                working_dir=Path(working_dir_raw).expanduser(),
                profile=profile,
                model=model,
                prompt=prompt,
                auto_approve=bool(auto_approve),
                team_id=team_id,
                project_id=project_id,
            )
        )
