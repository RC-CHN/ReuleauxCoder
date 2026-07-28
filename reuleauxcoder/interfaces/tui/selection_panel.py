"""Non-blocking selection panel state for the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass, field

from reuleauxcoder.app.commands.panels import PanelDefinition, PanelItem


@dataclass(slots=True)
class SelectionPanel:
    """Modal selection state: items plus the highlighted row."""

    title: str
    items: tuple[PanelItem, ...]
    index: int = 0
    view_type: str = field(default="", compare=False)
    definition: PanelDefinition | None = field(default=None, compare=False)

    @classmethod
    def open(
        cls,
        *,
        title: str,
        items: tuple[PanelItem, ...],
        view_type: str = "",
        definition: PanelDefinition | None = None,
    ) -> "SelectionPanel":
        index = next(
            (i for i, item in enumerate(items) if item.current),
            0,
        )
        return cls(
            title=title,
            items=items,
            index=index,
            view_type=view_type,
            definition=definition,
        )

    @classmethod
    def from_definition(cls, definition: PanelDefinition) -> "SelectionPanel":
        """Open mutable cursor state over an immutable command panel."""
        return cls.open(
            title=definition.title,
            items=definition.items,
            view_type=definition.view_type,
            definition=definition,
        )

    def move(self, delta: int) -> None:
        if self.items:
            self.index = (self.index + delta) % len(self.items)

    @property
    def selected(self) -> PanelItem | None:
        if not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    def refresh(self, definition: PanelDefinition) -> None:
        """Update items while keeping the highlight on the same label."""
        selected_label = self.selected.label if self.selected else None
        self.items = definition.items
        self.definition = definition
        self.title = definition.title
        self.view_type = definition.view_type
        items = definition.items
        if selected_label is not None:
            self.index = next(
                (i for i, item in enumerate(items) if item.label == selected_label),
                min(self.index, max(0, len(items) - 1)),
            )
        else:
            self.index = min(self.index, max(0, len(items) - 1))
