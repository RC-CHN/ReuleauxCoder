"""Framework-neutral presentation state shared by CLI and future TUI."""

from reuleauxcoder.presentation.composition import (
    TranscriptPlacement,
    compose_transcript,
)

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
    UserCell,
)
from reuleauxcoder.presentation.policy import (
    NotificationThreshold,
    PresentationPolicy,
    ReasoningDisplay,
    ToolOutputMode,
    Verbosity,
)
from reuleauxcoder.presentation.reducer import (
    PresentationChange,
    PresentationChangeKind,
    PresentationReducer,
    RuntimeViewState,
)
from reuleauxcoder.presentation.semantics import (
    DisplayTone,
    ToolInvocationDisplay,
    describe_tool_invocation,
)
from reuleauxcoder.presentation.execution import (
    AttentionItem,
    ExecutionAgentState,
    ExecutionPanelAgent,
    ExecutionPanelView,
    ExecutionPlanItem,
    ExecutionViewReducer,
    ExecutionViewState,
    execution_panel_lines,
    execution_panel_view,
)

__all__ = [
    "AttentionItem",
    "ExecutionAgentState",
    "ExecutionPanelAgent",
    "ExecutionPanelView",
    "ExecutionPlanItem",
    "ExecutionViewReducer",
    "ExecutionViewState",
    "execution_panel_lines",
    "execution_panel_view",
    "ApprovalCell",
    "AssistantCell",
    "DiagnosticCell",
    "DisplayTone",
    "DiffCell",
    "NoticeCell",
    "PresentationChange",
    "PresentationChangeKind",
    "PresentationPolicy",
    "NotificationThreshold",
    "ReasoningDisplay",
    "ToolOutputMode",
    "PresentationReducer",
    "RuntimeViewState",
    "SubagentCell",
    "ToolCell",
    "ToolCellStatus",
    "ToolInvocationDisplay",
    "TranscriptModel",
    "TranscriptPlacement",
    "UserCell",
    "Verbosity",
    "describe_tool_invocation",
    "compose_transcript",
]
