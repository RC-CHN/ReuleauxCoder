"""Deterministic reduction from RuntimeEvent to presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from reuleauxcoder.domain.runtime.events import (
    ChatCompleted,
    ChatStarted,
    ErrorOccurred,
    NotificationRaised,
    RuntimeEvent,
    StreamChunk,
    SubagentFinished,
    ToolCallFinished,
    ToolCallStarted,
)
from reuleauxcoder.presentation.models import (
    AssistantCell,
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
    session_generations: dict[tuple[str, str | None], int] = field(
        default_factory=dict
    )
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
        if isinstance(payload, ChatStarted):
            self._complete_active_assistant(event)
            return ()
        if isinstance(payload, StreamChunk):
            if payload.reasoning:
                return ()
            return self._append_stream(event, payload.text)
        if isinstance(payload, ChatCompleted):
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

    def _finish_tool(
        self, payload: ToolCallFinished
    ) -> tuple[PresentationChange, ...]:
        cell_id = f"tool:{payload.tool_call_id}"
        existing = self.state.transcript.get(cell_id)
        status = (
            ToolCellStatus.SUCCEEDED
            if payload.outcome.success
            else ToolCellStatus.FAILED
        )
        if isinstance(existing, ToolCell):
            updated = next_revision(
                existing, status=status, outcome=payload.outcome
            )
            return self._replace(updated)
        orphan = ToolCell(
            id=cell_id,
            tool_call_id=payload.tool_call_id,
            name=payload.tool_name,
            arguments=None,
            status=status,
            outcome=payload.outcome,
            orphaned=True,
        )
        return self._append(orphan)

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
