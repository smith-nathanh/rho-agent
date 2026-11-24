"""Search and compare models before selecting one for the session."""

from __future__ import annotations

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from ..client.catalog import CatalogModel
from .theme import PTK_STYLE


def _price(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.6g}"


async def pick_model(
    models: list[CatalogModel], *, current: str, openrouter: bool
) -> CatalogModel | None:
    """Open a searchable, keyboard-driven model list."""
    query = TextArea(height=1, prompt="Search › ", multiline=False)
    bindings = KeyBindings()
    matches: list[CatalogModel] = []
    selected = 0
    sort = "Name"
    tools_only = openrouter and any(model.supports_tools for model in models)

    def refresh() -> None:
        nonlocal matches, selected
        terms = query.text.casefold().split()
        matches = [
            model
            for model in models
            if (not tools_only or model.supports_tools)
            and all(term in f"{model.id} {model.name}".casefold() for term in terms)
        ]
        matches.sort(key=lambda model: (model.name or model.id).casefold())
        if sort != "Name":

            def price_key(model: CatalogModel) -> float:
                value = model.input_price if sort == "Input" else model.output_price
                return value if value is not None else float("inf")

            matches.sort(key=price_key)
        selected = 0

    def rows() -> list[tuple[str, str]]:
        if not matches:
            return [("class:prompt.muted", "  No matching models")]
        page_size = max(1, min(10, get_app().output.get_size().rows - 5))
        first = max(0, selected - page_size + 1)
        lines = []
        for index, model in enumerate(matches[first : first + page_size], start=first):
            marker = "›" if index == selected else " "
            style = "class:completion-menu.completion.current" if index == selected else ""
            label = model.id
            if model.name and model.name != model.id:
                label += f"  ·  {model.name}"
            if model.id == current:
                label += " (current)"
            lines.append(
                (
                    style,
                    f"{marker} {_price(model.input_price):>11} "
                    f"{_price(model.output_price):>11}  {label}\n",
                )
            )
        return lines

    def footer() -> str:
        capability = f" · F4 {'All models' if tools_only else 'Tool models'}" if openrouter else ""
        return f"{len(matches)} models · {sort} sort · F2 Input · F3 Output{capability}"

    @bindings.add("up")
    def previous(event: KeyPressEvent) -> None:
        nonlocal selected
        selected = max(0, selected - 1)

    @bindings.add("down")
    def following(event: KeyPressEvent) -> None:
        nonlocal selected
        selected = max(0, min(len(matches) - 1, selected + 1))

    @bindings.add("enter")
    def confirm(event: KeyPressEvent) -> None:
        if matches:
            event.app.exit(result=matches[selected])

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    @bindings.add("f2")
    @bindings.add("f3")
    def sort_by_price(event: KeyPressEvent) -> None:
        nonlocal sort
        field = "Input" if event.key_sequence[-1].key == "f2" else "Output"
        sort = "Name" if sort == field else field
        refresh()

    @bindings.add("f4")
    def toggle_tools(event: KeyPressEvent) -> None:
        nonlocal tools_only
        if openrouter:
            tools_only = not tools_only
            refresh()

    query.buffer.on_text_changed += lambda _: refresh()
    refresh()
    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(f"Select model · Current: {current}"), height=1),
                query,
                Window(
                    FormattedTextControl("        Input      Output  Model  ·  USD / 1M tokens"),
                    height=1,
                ),
                Window(FormattedTextControl(rows), wrap_lines=False),
                Window(FormattedTextControl(footer), height=1, wrap_lines=False),
                Window(
                    FormattedTextControl(
                        "↑/↓ Move · Enter Select · Esc Cancel · Prices are estimates"
                    ),
                    height=1,
                    wrap_lines=False,
                ),
            ]
        ),
        focused_element=query,
    )
    app: Application[CatalogModel | None] = Application(
        layout=layout, key_bindings=bindings, style=PTK_STYLE, erase_when_done=True
    )
    return await app.run_async()
