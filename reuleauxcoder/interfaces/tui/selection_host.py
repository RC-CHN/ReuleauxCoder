"""Generic host for command-owned interactive selection panels."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.formatted_text import FormattedText

from reuleauxcoder.app.commands.panels import (
    CommandPanelRegistry,
    PanelItem,
    PanelRefreshPolicy,
)
from reuleauxcoder.interfaces.tui.selection_panel import SelectionPanel


class SelectionHost:
    """Own modal cursor state while command features own panel definitions."""

    def __init__(
        self,
        *,
        registry: CommandPanelRegistry,
        input_text: Callable[[], str],
        submit_command: Callable[[str], None],
        invalidate: Callable[[], None],
    ) -> None:
        self.registry = registry
        self._input_text = input_text
        self._submit_command = submit_command
        self._invalidate = invalidate
        self.selection: SelectionPanel | None = None
        self.stack: list[SelectionPanel] = []

    @property
    def active(self) -> bool:
        return self.selection is not None

    @property
    def filterable(self) -> bool:
        panel = self.selection
        return bool(
            panel is not None
            and panel.definition is not None
            and panel.definition.filterable
        )

    def open_view(self, payload) -> bool:
        """Claim a command-owned view as a modal panel or absorb its refresh."""
        spec = self.registry.get(payload.view_type)
        if spec is None:
            return False
        definition = spec.build_for(payload.view_model, payload.title)
        if definition is None:
            return False

        is_refresh = payload.action == "refresh" or not payload.focus
        if is_refresh:
            if (
                spec.refresh is PanelRefreshPolicy.UPDATE
                and self.selection is not None
                and self.selection.view_type == definition.view_type
            ):
                self.selection.refresh(definition)
                self._invalidate()
            return True

        self.selection = SelectionPanel.from_definition(definition)
        self.stack = []
        self._invalidate()
        return True

    def visible_items(self) -> tuple[PanelItem, ...]:
        """Return panel rows after applying an optional live text filter."""
        panel = self.selection
        if panel is None:
            return ()
        definition = panel.definition
        if definition is None or not definition.filterable:
            return panel.items
        needle = self._input_text().strip().lower()
        if not needle:
            return panel.items
        return tuple(
            item
            for item in panel.items
            if needle in f"{item.label} {item.description}".lower()
        )

    def height(self) -> int:
        if self.selection is None:
            return 0
        return min(9, len(self.visible_items()) + 1)

    def move(self, delta: int) -> None:
        panel = self.selection
        if panel is None:
            return
        visible = len(self.visible_items())
        if visible:
            panel.index = (panel.index + delta) % visible
        self._invalidate()

    def close(self) -> None:
        if self.stack:
            self.selection = self.stack.pop()
        else:
            self.selection = None
        self._invalidate()

    def confirm(self) -> None:
        panel = self.selection
        if panel is None or panel.definition is None:
            return
        items = self.visible_items()
        if not items:
            return
        selected = items[min(panel.index, len(items) - 1)]
        child = panel.definition.child_for(selected.label)
        if child is not None:
            self.stack.append(panel)
            self.selection = SelectionPanel.from_definition(child)
            self._invalidate()
            return
        if not selected.command:
            return
        if not panel.definition.keep_open_on_submit:
            if panel.definition.return_to_parent_on_submit:
                self.selection = self.stack.pop() if self.stack else None
            else:
                self.selection = None
                self.stack = []
        self._submit_command(selected.command)

    def text(self) -> FormattedText:
        panel = self.selection
        if panel is None:
            return FormattedText([])
        items = self.visible_items()
        hint = " · type to filter" if self.filterable else ""
        fragments: list[tuple[str, str]] = [
            ("class:popup.cmd", f" {panel.title} "),
            ("class:popup", f"· Enter select{hint} · Esc close\n"),
        ]
        if not items:
            fragments.append(("class:popup", "  (no matches)\n"))
            return FormattedText(fragments)
        limit = 8
        index = min(panel.index, max(0, len(items) - 1))
        start = max(0, min(index - limit // 2, max(0, len(items) - limit)))
        for offset, item in enumerate(items[start : start + limit]):
            i = start + offset
            marker = "›" if i == index else " "
            current = " (current)" if item.current else ""
            row = f" {marker} {item.label}{current}"
            pad = " " * max(1, 24 - len(row))
            if i == index:
                fragments.append(
                    ("class:popup.selected", row + pad + item.description + "\n")
                )
            else:
                fragments.append(("class:popup.cmd", row))
                fragments.append(("class:popup", pad + item.description + "\n"))
        return FormattedText(fragments)


__all__ = ["SelectionHost"]
