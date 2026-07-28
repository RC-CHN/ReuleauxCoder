from reuleauxcoder.app.commands.panels import PanelDefinition, PanelItem
from reuleauxcoder.interfaces.tui.selection_panel import SelectionPanel


def _items() -> tuple[PanelItem, ...]:
    return (
        PanelItem("coder", "Default coding mode", "/mode switch coder", True),
        PanelItem("plan", "Planning first", "/mode switch plan"),
        PanelItem("debug", "Debugging", "/mode switch debug"),
    )


def test_open_starts_on_current_item() -> None:
    panel = SelectionPanel.open(title="Modes", items=_items())

    assert panel.index == 0
    assert panel.selected.label == "coder"
    assert panel.selected.command == "/mode switch coder"


def test_move_wraps_around() -> None:
    panel = SelectionPanel.open(title="Modes", items=_items())

    panel.move(-1)
    assert panel.selected.label == "debug"
    panel.move(1)
    assert panel.selected.label == "coder"


def test_refresh_keeps_highlight_on_same_label() -> None:
    panel = SelectionPanel.open(title="Modes", items=_items())
    panel.move(1)
    assert panel.selected.label == "plan"

    refreshed = (
        PanelItem("coder", "Default coding mode", "/mode switch coder"),
        PanelItem("debug", "Debugging", "/mode switch debug"),
        PanelItem("plan", "Planning first", "/mode switch plan", True),
    )
    panel.refresh(PanelDefinition("mode_profiles", "Modes", refreshed))

    assert panel.selected.label == "plan"


def test_refresh_falls_back_when_label_disappears() -> None:
    panel = SelectionPanel.open(title="Modes", items=_items())
    panel.move(2)
    assert panel.selected.label == "debug"

    panel.refresh(
        PanelDefinition(
            "mode_profiles",
            "Modes",
            (PanelItem("coder", "only", "/mode switch coder"),),
        )
    )

    assert panel.selected.label == "coder"
    assert panel.index == 0
