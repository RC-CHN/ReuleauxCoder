"""Non-blocking selection panel state for the mini-TUI (UI-neutral)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SelectionItem:
    """One row in a selection panel.

    ``command`` is the canonical slash command resubmitted on confirm; the
    panel never mutates agent state directly.
    """

    label: str
    description: str
    command: str
    current: bool = False


@dataclass(slots=True)
class SelectionPanel:
    """Modal selection state: items plus the highlighted row."""

    title: str
    items: tuple[SelectionItem, ...]
    index: int = 0
    view_type: str = field(default="", compare=False)

    @classmethod
    def open(
        cls,
        *,
        title: str,
        items: tuple[SelectionItem, ...],
        view_type: str = "",
    ) -> "SelectionPanel":
        index = next(
            (i for i, item in enumerate(items) if item.current),
            0,
        )
        return cls(title=title, items=items, index=index, view_type=view_type)

    def move(self, delta: int) -> None:
        if self.items:
            self.index = (self.index + delta) % len(self.items)

    @property
    def selected(self) -> SelectionItem | None:
        if not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    def refresh(self, items: tuple[SelectionItem, ...]) -> None:
        """Update items while keeping the highlight on the same label."""
        selected_label = self.selected.label if self.selected else None
        self.items = items
        if selected_label is not None:
            self.index = next(
                (i for i, item in enumerate(items) if item.label == selected_label),
                min(self.index, max(0, len(items) - 1)),
            )
        else:
            self.index = min(self.index, max(0, len(items) - 1))
