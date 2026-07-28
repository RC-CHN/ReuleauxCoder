"""Virtual visual-line index for the Prompt Toolkit transcript viewport."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text.base import StyleAndTextTuples
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEvent


Fragments = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class VisualCell:
    key: tuple[str, int, int, int]
    lines: tuple[Fragments, ...]


class VirtualTranscriptLayout:
    """Immutable cell/line index; lines are resolved lazily by viewport row."""

    def __init__(self, cells: tuple[VisualCell, ...]) -> None:
        self.cells = cells
        starts: list[int] = []
        total = 0
        for cell in cells:
            starts.append(total)
            total += len(cell.lines)
        self._starts = tuple(starts)
        self._cell_indexes = {cell.key[0]: index for index, cell in enumerate(cells)}
        self.line_count = max(1, total)

    def anchor_at(self, line_number: int) -> tuple[str, int] | None:
        """Return the stable cell/local-line identity at one visual row."""
        if not self.cells:
            return None
        line_number = max(0, min(line_number, self.line_count - 1))
        cell_index = bisect_right(self._starts, line_number) - 1
        cell_index = max(0, cell_index)
        return (
            self.cells[cell_index].key[0],
            line_number - self._starts[cell_index],
        )

    def with_replacements(
        self, replacements: dict[str, VisualCell]
    ) -> "VirtualTranscriptLayout":
        """Return a new index with only the named visual cells replaced."""
        if not replacements:
            return self
        cells = tuple(replacements.get(cell.key[0], cell) for cell in self.cells)
        return VirtualTranscriptLayout(cells)

    def cell(self, cell_id: str) -> VisualCell | None:
        index = self._cell_indexes.get(cell_id)
        return self.cells[index] if index is not None else None

    def line_for_anchor(
        self,
        anchor: tuple[str, int] | None,
        *,
        fallback: int = 0,
    ) -> int:
        """Resolve a prior cell/local-line anchor in this layout revision."""
        if not self.cells or anchor is None:
            return max(0, min(fallback, self.line_count - 1))
        cell_id, local_line = anchor
        cell_index = self._cell_indexes.get(cell_id)
        if cell_index is None:
            return max(0, min(fallback, self.line_count - 1))
        cell = self.cells[cell_index]
        bounded_local = max(0, min(local_line, max(0, len(cell.lines) - 1)))
        return self._starts[cell_index] + bounded_local

    def get_line(self, line_number: int) -> StyleAndTextTuples:
        if not self.cells or line_number < 0 or line_number >= self.line_count:
            return [("class:muted", "No activity yet.")] if not self.cells else []
        cell_index = bisect_right(self._starts, line_number) - 1
        cell = self.cells[cell_index]
        local_line = line_number - self._starts[cell_index]
        return list(cell.lines[local_line])

    def flatten(self) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        for line_number in range(self.line_count):
            fragments.extend(self.get_line(line_number))
            if line_number + 1 < self.line_count:
                fragments.append(("", "\n"))
        return fragments


class VirtualTranscriptControl(UIControl):
    """Expose only requested viewport rows to prompt_toolkit's Window."""

    def __init__(
        self,
        layout_provider: Callable[[int], VirtualTranscriptLayout],
        cursor_provider: Callable[[], Point],
        mouse_handler: Callable[[MouseEvent], object] | None = None,
    ) -> None:
        self._layout_provider = layout_provider
        self._cursor_provider = cursor_provider
        self._mouse_handler = mouse_handler
        self.last_width = 0
        self.last_height = 0
        self.last_line_count = 0

    def create_content(self, width: int, height: int) -> UIContent:
        layout = self._layout_provider(width)
        self.last_width = width
        self.last_height = height
        self.last_line_count = layout.line_count
        cursor = self._cursor_provider()
        cursor = Point(
            x=0,
            y=max(0, min(cursor.y, layout.line_count - 1)),
        )
        return UIContent(
            get_line=layout.get_line,
            line_count=layout.line_count,
            cursor_position=cursor,
            show_cursor=False,
        )

    def is_focusable(self) -> bool:
        return False

    def mouse_handler(self, mouse_event: MouseEvent):
        if self._mouse_handler is not None:
            return self._mouse_handler(mouse_event)
        return NotImplemented
