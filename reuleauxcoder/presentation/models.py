"""Typed, UI-framework-neutral transcript cells."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TypeAlias

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.approval import ApprovalSection
from reuleauxcoder.domain.runtime.events import RuntimeDiagnostic


@dataclass(frozen=True)
class AssistantCell:
    id: str
    text: str = ""
    complete: bool = False
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class UserCell:
    id: str
    text: str
    revision: int = 0
    group_id: str | None = None


class ToolCellStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ToolCell:
    id: str
    tool_call_id: str
    name: str
    arguments: dict | None
    output: str = ""
    status: ToolCellStatus = ToolCellStatus.RUNNING
    outcome: ToolOutcome | None = None
    orphaned: bool = False
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class DiffCell:
    id: str
    path: str | None
    diff: str
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class DiagnosticCell:
    id: str
    path: str
    batch_id: str
    document_version: int
    diagnostic_generation: int
    diagnostics: tuple[RuntimeDiagnostic, ...]
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class SubagentCell:
    id: str
    job_id: str
    mode: str
    task: str
    status: str
    result: str | None = None
    error: str | None = None
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class NoticeCell:
    id: str
    message: str
    level: str = "info"
    category: str = "system"
    revision: int = 0
    group_id: str | None = None


@dataclass(frozen=True)
class ApprovalCell:
    id: str
    request_id: str
    title: str
    status: str
    preview: str | None = None
    summary: str = ""
    sections: tuple[ApprovalSection, ...] = ()
    reason: str | None = None
    revision: int = 0
    group_id: str | None = None


TranscriptCell: TypeAlias = (
    UserCell
    | AssistantCell
    | ToolCell
    | DiffCell
    | DiagnosticCell
    | SubagentCell
    | NoticeCell
    | ApprovalCell
)


class TranscriptModel:
    """Bounded ordered transcript with stable cell identity."""

    def __init__(self, *, max_cells: int = 500, max_text_chars: int = 1_000_000):
        if max_cells < 1:
            raise ValueError("max_cells must be positive")
        if max_text_chars < 1:
            raise ValueError("max_text_chars must be positive")
        self.max_cells = max_cells
        self.max_text_chars = max_text_chars
        self._cells: list[TranscriptCell] = []
        self._indexes: dict[str, int] = {}
        self._text_chars_total = 0
        self._revision = 0
        self._cells_snapshot: tuple[TranscriptCell, ...] = ()

    @property
    def revision(self) -> int:
        """Monotonic source revision for cheap retained-view invalidation."""
        return self._revision

    @property
    def cells(self) -> tuple[TranscriptCell, ...]:
        if len(self._cells_snapshot) != len(self._cells):
            self._cells_snapshot = tuple(self._cells)
        return self._cells_snapshot

    def get(self, cell_id: str) -> TranscriptCell | None:
        index = self._indexes.get(cell_id)
        return self._cells[index] if index is not None else None

    def append(self, cell: TranscriptCell) -> tuple[TranscriptCell, ...]:
        if cell.id in self._indexes:
            raise ValueError(f"Duplicate transcript cell id: {cell.id}")
        self._indexes[cell.id] = len(self._cells)
        self._cells.append(cell)
        self._text_chars_total += self._cell_text_chars(cell)
        self._mark_changed()
        return self._enforce_retention()

    def replace(self, cell: TranscriptCell) -> TranscriptCell:
        index = self._indexes.get(cell.id)
        if index is None:
            raise KeyError(cell.id)
        previous = self._cells[index]
        self._cells[index] = cell
        self._text_chars_total += self._cell_text_chars(
            cell
        ) - self._cell_text_chars(previous)
        self._mark_changed()
        self._enforce_retention()
        return previous

    def clear(self) -> None:
        if not self._cells:
            return
        self._cells.clear()
        self._indexes.clear()
        self._text_chars_total = 0
        self._mark_changed()

    def _enforce_retention(self) -> tuple[TranscriptCell, ...]:
        evicted: list[TranscriptCell] = []
        while (
            len(self._cells) > self.max_cells
            or self._text_chars_total > self.max_text_chars
        ):
            cell = self._cells.pop(0)
            self._text_chars_total -= self._cell_text_chars(cell)
            evicted.append(cell)
        if evicted:
            self._reindex()
        return tuple(evicted)

    def _reindex(self) -> None:
        self._indexes = {cell.id: index for index, cell in enumerate(self._cells)}

    def _mark_changed(self) -> None:
        self._revision += 1
        self._cells_snapshot = ()

    @staticmethod
    def _cell_text_chars(cell: TranscriptCell) -> int:
        if isinstance(cell, (UserCell, AssistantCell)):
            return len(cell.text)
        if isinstance(cell, ToolCell) and cell.outcome is not None:
            return len(cell.outcome.display_text)
        if isinstance(cell, NoticeCell):
            return len(cell.message)
        if isinstance(cell, SubagentCell):
            return len(cell.result or "") + len(cell.error or "")
        if isinstance(cell, DiffCell):
            return len(cell.diff)
        return 0


def next_revision(cell: TranscriptCell, **changes) -> TranscriptCell:
    """Return a cell replacement with a monotonically increasing revision."""
    return replace(cell, revision=cell.revision + 1, **changes)
