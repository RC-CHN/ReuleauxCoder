"""Deterministic reduction from RuntimeEvent to presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantContentDelta,
    ChatCompleted,
    ChatStarted,
    DiagnosticsCleared,
    DiagnosticsPublished,
    ErrorOccurred,
    NotificationRaised,
    ReasoningDelta,
    RuntimeStateChanged,
    RuntimeEvent,
    SessionChanged,
    StreamChunk,
    SubagentFinished,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
    TurnFinished,
    TurnStarted,
    ViewRefreshed,
    ViewRequested,
)
from reuleauxcoder.presentation.models import (
    AssistantCell,
    ApprovalCell,
    DiagnosticCell,
    DiffCell,
    NoticeCell,
    SubagentCell,
    ToolCell,
    ToolCellStatus,
    TranscriptCell,
    TranscriptModel,
    next_revision,
)
from reuleauxcoder.presentation.policy import PresentationPolicy


class PresentationChangeKind(str, Enum):
    APPEND = "append"
    UPDATE = "update"
    EVICT = "evict"
    RESET = "reset"


@dataclass(frozen=True)
class PresentationChange:
    kind: PresentationChangeKind
    cell: TranscriptCell | None = None
    previous: TranscriptCell | None = None


@dataclass
class RuntimeViewState:
    transcript: TranscriptModel = field(default_factory=TranscriptModel)
    seen_event_ids: set[str] = field(default_factory=set)
    session_generations: dict[tuple[str, str | None], int] = field(default_factory=dict)
    active_session_id: str | None = None
    runtime_state: str = "idle"
    view_revisions: dict[tuple[str, str], int] = field(default_factory=dict)
    active_assistant_cells: dict[
        tuple[str | None, str | None, int | None, str | None], str
    ] = field(default_factory=dict)


class PresentationReducer:
    """Owns correlation and retention; renderers only map typed changes to UI."""

    def __init__(
        self,
        *,
        state: RuntimeViewState | None = None,
        policy: PresentationPolicy | None = None,
    ):
        self.state = state or RuntimeViewState()
        self.policy = policy or PresentationPolicy()

    def apply(self, event: RuntimeEvent) -> tuple[PresentationChange, ...]:
        if event.event_id in self.state.seen_event_ids:
            return ()
        self.state.seen_event_ids.add(event.event_id)
        if self._is_stale_generation(event):
            return ()

        payload = event.payload
        if isinstance(payload, (TurnStarted, ChatStarted)):
            self._complete_active_assistant(event)
            return ()
        if isinstance(payload, AssistantContentDelta):
            return self._append_stream(event, payload.text)
        if isinstance(payload, ReasoningDelta):
            return ()
        if isinstance(payload, StreamChunk):
            if payload.reasoning:
                return ()
            return self._append_stream(event, payload.text)
        if isinstance(payload, (TurnFinished, ChatCompleted)):
            return self._complete_chat(event, payload)
        if isinstance(payload, ToolCallStarted):
            cell = ToolCell(
                id=f"tool:{payload.tool_call_id}",
                tool_call_id=payload.tool_call_id,
                name=payload.tool_name,
                arguments=payload.arguments,
            )
            return self._append(cell)
        if isinstance(payload, ToolCallFinished):
            return self._finish_tool(payload)
        if isinstance(payload, ToolOutputDelta):
            return self._append_tool_output(payload)
        if isinstance(payload, SubagentFinished):
            cell = SubagentCell(
                id=f"subagent:{payload.job_id}",
                job_id=payload.job_id,
                mode=payload.mode,
                task=payload.task,
                status=payload.status,
                result=payload.result,
                error=payload.error,
            )
            existing = self.state.transcript.get(cell.id)
            if isinstance(existing, SubagentCell):
                updated = next_revision(
                    existing,
                    mode=cell.mode,
                    task=cell.task,
                    status=cell.status,
                    result=cell.result,
                    error=cell.error,
                )
                return self._replace(updated)
            return self._append(cell)
        if isinstance(payload, ErrorOccurred):
            return self._append(
                NoticeCell(
                    id=f"notice:{event.event_id}",
                    message=payload.message,
                    level="error",
                    category="agent",
                )
            )
        if isinstance(payload, NotificationRaised):
            return self._append(
                NoticeCell(
                    id=f"notice:{event.event_id}",
                    message=payload.message,
                    level=payload.severity,
                    category=payload.code,
                )
            )
        if isinstance(payload, DiagnosticsPublished):
            return self._publish_diagnostics(event, payload)
        if isinstance(payload, DiagnosticsCleared):
            return self._clear_diagnostics(event, payload)
        if isinstance(payload, ApprovalRequested):
            return self._request_approval(payload)
        if isinstance(payload, ApprovalResolved):
            return self._resolve_approval(payload)
        if isinstance(payload, SessionChanged):
            self.state.active_session_id = payload.session_id
            return ()
        if isinstance(payload, RuntimeStateChanged):
            self.state.runtime_state = payload.state
            return ()
        if isinstance(payload, ViewRequested):
            self.state.view_revisions[(payload.request_id, payload.view_type)] = 0
            return ()
        if isinstance(payload, ViewRefreshed):
            self.state.view_revisions[(payload.request_id, payload.view_type)] = (
                payload.revision
            )
            return ()
        raise TypeError(f"Unsupported runtime payload: {type(payload).__name__}")

    def _is_stale_generation(self, event: RuntimeEvent) -> bool:
        if event.agent_id is None or event.session_generation is None:
            return False
        key = (event.agent_id, event.session_id)
        current = self.state.session_generations.get(key)
        if current is not None and event.session_generation < current:
            return True
        if current is None or event.session_generation > current:
            self.state.session_generations[key] = event.session_generation
        return False

    def append_notice(
        self,
        *,
        notice_id: str,
        message: str,
        level: str = "info",
        category: str = "system",
    ) -> tuple[PresentationChange, ...]:
        """Compatibility boundary for legacy interface-only notifications."""
        cell_id = f"notice:{notice_id}"
        if self.state.transcript.get(cell_id) is not None:
            return ()
        return self._append(
            NoticeCell(
                id=cell_id,
                message=message,
                level=level,
                category=category,
            )
        )

    def _append_stream(
        self, event: RuntimeEvent, text: str
    ) -> tuple[PresentationChange, ...]:
        route = self._assistant_route(event)
        cell_id = self.state.active_assistant_cells.get(route)
        existing = self.state.transcript.get(cell_id) if cell_id else None
        if not isinstance(existing, AssistantCell) or existing.complete:
            identity = event.turn_id or event.event_id
            cell_id = (
                f"assistant:{event.agent_id}:{identity}"
                if event.agent_id
                else f"assistant:{identity}"
            )
            existing_same_id = self.state.transcript.get(cell_id)
            if isinstance(existing_same_id, AssistantCell):
                cell_id = f"{cell_id}:{event.event_id}"
            cell = AssistantCell(id=cell_id, text=text)
            self.state.active_assistant_cells[route] = cell_id
            return self._append(cell)
        updated = next_revision(existing, text=existing.text + text)
        return self._replace(updated)

    def _complete_chat(
        self, event: RuntimeEvent, payload: ChatCompleted
    ) -> tuple[PresentationChange, ...]:
        route = self._assistant_route(event)
        cell_id = self.state.active_assistant_cells.get(route)
        existing = self.state.transcript.get(cell_id) if cell_id else None
        if isinstance(existing, AssistantCell) and not existing.complete:
            updated = next_revision(existing, complete=True)
            self.state.active_assistant_cells.pop(route, None)
            return self._replace(updated)
        self.state.active_assistant_cells.pop(route, None)
        if payload.response and payload.render_response:
            identity = event.turn_id or event.event_id
            return self._append(
                AssistantCell(
                    id=(
                        f"assistant:{event.agent_id}:{identity}"
                        if event.agent_id
                        else f"assistant:{identity}"
                    ),
                    text=payload.response,
                    complete=True,
                )
            )
        return ()

    def _complete_active_assistant(self, event: RuntimeEvent) -> None:
        route = self._assistant_route(event)
        cell_id = self.state.active_assistant_cells.get(route)
        existing = self.state.transcript.get(cell_id) if cell_id else None
        if isinstance(existing, AssistantCell) and not existing.complete:
            self.state.transcript.replace(next_revision(existing, complete=True))
        self.state.active_assistant_cells.pop(route, None)

    @staticmethod
    def _assistant_route(
        event: RuntimeEvent,
    ) -> tuple[str | None, str | None, int | None, str | None]:
        return (
            event.agent_id,
            event.session_id,
            event.session_generation,
            event.turn_id,
        )

    def _finish_tool(self, payload: ToolCallFinished) -> tuple[PresentationChange, ...]:
        cell_id = f"tool:{payload.tool_call_id}"
        existing = self.state.transcript.get(cell_id)
        status = (
            ToolCellStatus.SUCCEEDED
            if payload.outcome.success
            else ToolCellStatus.FAILED
        )
        if isinstance(existing, ToolCell):
            updated = next_revision(existing, status=status, outcome=payload.outcome)
            changes = self._replace(updated)
        else:
            orphan = ToolCell(
                id=cell_id,
                tool_call_id=payload.tool_call_id,
                name=payload.tool_name,
                arguments=None,
                status=status,
                outcome=payload.outcome,
                orphaned=True,
            )
            changes = self._append(orphan)
        return changes + self._record_diff(payload)

    def _record_diff(self, payload: ToolCallFinished) -> tuple[PresentationChange, ...]:
        diff = payload.outcome.diff
        if diff is None or not diff.unified:
            return ()
        cell_id = f"diff:{payload.tool_call_id}"
        existing = self.state.transcript.get(cell_id)
        if isinstance(existing, DiffCell):
            return self._replace(
                next_revision(existing, path=diff.path, diff=diff.unified)
            )
        return self._append(DiffCell(id=cell_id, path=diff.path, diff=diff.unified))

    def _append_tool_output(
        self, payload: ToolOutputDelta
    ) -> tuple[PresentationChange, ...]:
        cell_id = f"tool:{payload.tool_call_id}"
        existing = self.state.transcript.get(cell_id)
        if isinstance(existing, ToolCell):
            return self._replace(
                next_revision(existing, output=existing.output + payload.text)
            )
        return self._append(
            ToolCell(
                id=cell_id,
                tool_call_id=payload.tool_call_id,
                name="unknown_tool",
                arguments=None,
                output=payload.text,
                orphaned=True,
            )
        )

    @staticmethod
    def _diagnostic_cell_id(event: RuntimeEvent, file_path: str) -> str:
        owner = event.agent_id or "unknown"
        return f"diagnostic:{owner}:{file_path}"

    def _publish_diagnostics(
        self, event: RuntimeEvent, payload: DiagnosticsPublished
    ) -> tuple[PresentationChange, ...]:
        cell = DiagnosticCell(
            id=self._diagnostic_cell_id(event, payload.file_path),
            path=payload.file_path,
            batch_id=payload.batch_id,
            document_version=payload.document_version,
            diagnostic_generation=payload.diagnostic_generation,
            diagnostics=payload.diagnostics,
        )
        existing = self.state.transcript.get(cell.id)
        if isinstance(existing, DiagnosticCell):
            return self._replace(
                next_revision(
                    existing,
                    batch_id=cell.batch_id,
                    document_version=cell.document_version,
                    diagnostic_generation=cell.diagnostic_generation,
                    diagnostics=cell.diagnostics,
                )
            )
        return self._append(cell)

    def _clear_diagnostics(
        self, event: RuntimeEvent, payload: DiagnosticsCleared
    ) -> tuple[PresentationChange, ...]:
        return self._publish_diagnostics(
            event,
            DiagnosticsPublished(
                batch_id=payload.batch_id,
                file_path=payload.file_path,
                document_version=payload.document_version,
                diagnostic_generation=payload.diagnostic_generation,
                diagnostics=(),
            ),
        )

    def _request_approval(
        self, payload: ApprovalRequested
    ) -> tuple[PresentationChange, ...]:
        cell = ApprovalCell(
            id=f"approval:{payload.request_id}",
            request_id=payload.request_id,
            title=payload.title,
            status="pending",
            preview=payload.preview,
        )
        existing = self.state.transcript.get(cell.id)
        if isinstance(existing, ApprovalCell):
            return self._replace(
                next_revision(
                    existing,
                    title=payload.title,
                    status="pending",
                    preview=payload.preview,
                    reason=None,
                )
            )
        return self._append(cell)

    def _resolve_approval(
        self, payload: ApprovalResolved
    ) -> tuple[PresentationChange, ...]:
        cell_id = f"approval:{payload.request_id}"
        existing = self.state.transcript.get(cell_id)
        status = "approved" if payload.approved else "denied"
        if isinstance(existing, ApprovalCell):
            return self._replace(
                next_revision(existing, status=status, reason=payload.reason)
            )
        return self._append(
            ApprovalCell(
                id=cell_id,
                request_id=payload.request_id,
                title="Approval",
                status=status,
                reason=payload.reason,
            )
        )

    def _append(self, cell: TranscriptCell) -> tuple[PresentationChange, ...]:
        evicted = self.state.transcript.append(cell)
        changes = [PresentationChange(PresentationChangeKind.APPEND, cell=cell)]
        changes.extend(
            PresentationChange(PresentationChangeKind.EVICT, cell=old)
            for old in evicted
        )
        return tuple(changes)

    def _replace(self, cell: TranscriptCell) -> tuple[PresentationChange, ...]:
        previous = self.state.transcript.replace(cell)
        return (
            PresentationChange(
                PresentationChangeKind.UPDATE, cell=cell, previous=previous
            ),
        )
