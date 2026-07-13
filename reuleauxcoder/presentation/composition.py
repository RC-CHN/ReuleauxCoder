"""Framework-neutral transcript grouping and spacing decisions."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.presentation.models import (
    AssistantCell,
    DiagnosticCell,
    NoticeCell,
    TranscriptCell,
    UserCell,
)


@dataclass(frozen=True, slots=True)
class TranscriptPlacement:
    cell: TranscriptCell
    begins_turn: bool = False
    show_assistant_label: bool = False
    blank_lines_after: int = 1

    @property
    def decoration_key(self) -> str:
        return (
            f"turn={int(self.begins_turn)};"
            f"assistant={int(self.show_assistant_label)};"
            f"gap={self.blank_lines_after}"
        )


def compose_transcript(
    cells: tuple[TranscriptCell, ...],
) -> tuple[TranscriptPlacement, ...]:
    """Group cells into human turns without embedding terminal styling."""
    assistant_groups: set[str] = set()
    placements: list[TranscriptPlacement] = []
    for index, cell in enumerate(cells):
        group_key = cell.group_id or f"cell:{cell.id}"
        show_assistant = isinstance(cell, AssistantCell) and group_key not in assistant_groups
        if isinstance(cell, AssistantCell):
            assistant_groups.add(group_key)
        compact_notice = isinstance(cell, (NoticeCell, DiagnosticCell)) and not isinstance(
            cell, UserCell
        )
        placements.append(
            TranscriptPlacement(
                cell=cell,
                begins_turn=isinstance(cell, UserCell) and index > 0,
                show_assistant_label=show_assistant,
                blank_lines_after=0 if compact_notice else 1,
            )
        )
    return tuple(placements)
