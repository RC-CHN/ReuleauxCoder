"""Framework-neutral presentation state shared by CLI and future TUI."""

from reuleauxcoder.presentation.models import (
    ApprovalCell,
    AssistantCell,
    DiagnosticCell,
    DiffCell,
    NoticeCell,
    SubagentCell,
    ToolCell,
    ToolCellStatus,
    TranscriptModel,
)
from reuleauxcoder.presentation.policy import PresentationPolicy, Verbosity
from reuleauxcoder.presentation.reducer import (
    PresentationChange,
    PresentationChangeKind,
    PresentationReducer,
    RuntimeViewState,
)

__all__ = [
    "ApprovalCell",
    "AssistantCell",
    "DiagnosticCell",
    "DiffCell",
    "NoticeCell",
    "PresentationChange",
    "PresentationChangeKind",
    "PresentationPolicy",
    "PresentationReducer",
    "RuntimeViewState",
    "SubagentCell",
    "ToolCell",
    "ToolCellStatus",
    "TranscriptModel",
    "Verbosity",
]
