"""Thread-safe event projection and virtual transcript state for the TUI."""

from __future__ import annotations

from dataclasses import replace
import queue
import threading
import time
from typing import Callable

from prompt_toolkit.formatted_text import FormattedText

from reuleauxcoder.app.commands.view_models import SessionResumeViewModel
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantContentDelta,
    PlanUpdated,
    ProgressReported,
    ReasoningDelta,
    RuntimeEvent,
    StreamChunk,
    SubagentJobChanged,
)
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeEventPayload,
    UIEvent,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.interactions import ReviewRequest
from reuleauxcoder.interfaces.tui.formatting import (
    fragments_to_visual_lines as _fragments_to_visual_lines,
    wrap_fragments as _wrap_fragments,
)
from reuleauxcoder.interfaces.tui.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.tui.transcript import (
    cell_fragments as _cell_fragments,
    decorate_transcript_fragments as _decorate_transcript_fragments,
)
from reuleauxcoder.interfaces.tui.view_text import view_text as _view_text
from reuleauxcoder.interfaces.tui.virtual_transcript import (
    VirtualTranscriptLayout,
    VisualCell,
)
from reuleauxcoder.presentation import (
    AssistantCell,
    ExecutionPanelView,
    ExecutionViewReducer,
    PresentationChangeKind,
    PresentationReducer,
    RuntimeViewState,
    TranscriptModel,
    TranscriptPlacement,
    UserCell,
    compose_transcript,
    execution_panel_lines,
    execution_panel_view,
)


_COALESCIBLE_STREAM_TYPES = (AssistantContentDelta, ReasoningDelta, StreamChunk)


def _stream_event_key(event: UIEvent) -> tuple | None:
    envelope = event.payload
    if not isinstance(envelope, RuntimeEventPayload):
        return None
    runtime = envelope.event
    payload = runtime.payload
    if not isinstance(payload, _COALESCIBLE_STREAM_TYPES):
        return None
    payload_variant = (
        getattr(payload, "reasoning", None),
        getattr(payload, "display_mode", None),
    )
    return (
        type(payload),
        runtime.agent_id,
        runtime.session_id,
        runtime.session_generation,
        runtime.turn_id,
        runtime.correlation_id,
        payload_variant,
    )


def _coalesce_stream_events(events: list[UIEvent]) -> list[UIEvent]:
    """Merge adjacent stream deltas already waiting for the same UI paint."""
    merged: list[UIEvent] = []
    current: UIEvent | None = None
    current_key: tuple | None = None
    text_parts: list[str] = []

    def flush() -> None:
        nonlocal current, current_key, text_parts
        if current is None:
            return
        if len(text_parts) > 1:
            envelope = current.payload
            assert isinstance(envelope, RuntimeEventPayload)
            runtime = envelope.event
            payload = replace(runtime.payload, text="".join(text_parts))
            current = replace(
                current,
                payload=RuntimeEventPayload(replace(runtime, payload=payload)),
            )
        merged.append(current)
        current = None
        current_key = None
        text_parts = []

    for event in events:
        key = _stream_event_key(event)
        if key is None:
            flush()
            merged.append(event)
            continue
        envelope = event.payload
        assert isinstance(envelope, RuntimeEventPayload)
        stream_payload = envelope.event.payload
        # _stream_event_key only admits _COALESCIBLE_STREAM_TYPES payloads.
        assert isinstance(stream_payload, _COALESCIBLE_STREAM_TYPES)
        if current is not None and key == current_key:
            current = event
            text_parts.append(stream_payload.text)
            continue
        flush()
        current = event
        current_key = key
        text_parts = [stream_payload.text]
    flush()
    return merged


class MiniTUIEventAdapter:
    """Thread-safe, source-backed event projection for the mini-TUI."""

    def __init__(self, *, root_agent_id: str | None = None) -> None:
        self.root_agent_id = root_agent_id
        # Optional hook: focused interactive views (selection panels) may be
        # claimed by the app instead of being projected as transcript notices.
        self.interactive_view_handler: (
            Callable[[ViewEventPayload], bool] | None
        ) = None
        self.runtime_event_handler: Callable[[RuntimeEvent], None] | None = None
        self.transcript = PresentationReducer(
            state=RuntimeViewState(
                transcript=TranscriptModel(
                    max_cells=2_000,
                    max_text_chars=2_000_000,
                )
            )
        )
        self.execution = ExecutionViewReducer(root_agent_id=root_agent_id)
        self._lock = threading.RLock()
        self._invalidate = lambda: None
        self._notice_seq = 0
        self._pending_events: queue.SimpleQueue[UIEvent] = queue.SimpleQueue()
        self._viewport_width = 100
        self._markdown = RetainedMarkdownRenderer()
        self._theme_revision = 0
        self._cell_visual_cache: dict[
            tuple[str, int, int, int], tuple[tuple[tuple[str, str], ...], ...]
        ] = {}
        self._transcript_layout_key: tuple[tuple[str, int, int, int], ...] = ()
        self._transcript_layout_source_key: tuple[int, int, int, int] | None = None
        self._transcript_layout = VirtualTranscriptLayout(())
        self._layout_model_revision = -1
        self._layout_structure_dirty = True
        self._layout_dirty_ids: set[str] = set()
        self._placement_by_id: dict[str, TranscriptPlacement] = {}
        self._flattened_layout: VirtualTranscriptLayout | None = None
        self._transcript_rendered = FormattedText()

    def bind_invalidator(self, callback) -> None:
        self._invalidate = callback

    def on_ui_event(self, event: UIEvent) -> None:
        # Worker/model/tool threads only enqueue. Projection and rendering are
        # drained by prompt_toolkit's UI thread on the next paint.
        self._pending_events.put(event)
        self._invalidate()

    def _drain_pending_events(self) -> None:
        with self._lock:
            pending: list[UIEvent] = []
            while True:
                try:
                    pending.append(self._pending_events.get_nowait())
                except queue.Empty:
                    break
            for event in _coalesce_stream_events(pending):
                try:
                    self._apply_pending_event_locked(event)
                except Exception as error:
                    # A malformed view projection must not stop the agent or the
                    # viewport. Keep one bounded diagnostic in the transcript.
                    self._notice_seq += 1
                    self._record_presentation_changes(
                        self.transcript.append_notice(
                            notice_id=f"ui-projection:{self._notice_seq}",
                            message=f"UI projection skipped: {error}",
                            level="warning",
                            category="ui",
                        )
                    )

    def _apply_pending_event_locked(self, event: UIEvent) -> None:
        if isinstance(event.payload, RuntimeEventPayload):
            runtime = event.payload.event
            handler = self.runtime_event_handler
            if handler is not None:
                handler(runtime)
            self.execution.apply(runtime)
            if self._is_root_transcript_event(runtime):
                self._record_presentation_changes(self.transcript.apply(runtime))
            return
        if isinstance(event.payload, InteractionPromptPayload):
            request = event.payload.request
            if isinstance(request, ReviewRequest):
                self._record_presentation_changes(
                    self.transcript.hydrate_approval(
                        request_id=request.request_id,
                        title=request.title,
                        summary=request.summary,
                        sections=request.sections,
                    )
                )
            return
        message = event.message
        if isinstance(event.payload, ViewEventPayload):
            # Interactive presenters (selection panels) own their view types,
            # for both opens and refreshes; they suppress passive projection.
            handler = self.interactive_view_handler
            if handler is not None and handler(event.payload):
                return
            if event.payload.view_type == "session_resume" and isinstance(
                event.payload.view_model, SessionResumeViewModel
            ):
                model = event.payload.view_model
                restored_group: str | None = None
                for index, entry in enumerate(model.entries):
                    cell_id = f"restored:{index}:{self._notice_seq}"
                    if entry.role == "user":
                        restored_group = f"restored-turn:{index}:{self._notice_seq}"
                        self.transcript.state.transcript.append(
                            UserCell(
                                id=cell_id,
                                text=entry.content,
                                group_id=restored_group,
                            )
                        )
                    elif entry.role == "assistant":
                        self.transcript.state.transcript.append(
                            AssistantCell(
                                id=cell_id,
                                text=entry.content,
                                complete=True,
                                group_id=restored_group or cell_id,
                            )
                        )
                self._notice_seq += 1
                message = (
                    f"RESTORED {model.session_id} · {model.model} · "
                    f"{model.saved_at[:19]}"
                )
            else:
                message = _view_text(event.payload)
        if message:
            self._notice_seq += 1
            self._record_presentation_changes(
                self.transcript.append_notice(
                    notice_id=f"ui:{event.timestamp}:{self._notice_seq}",
                    message=message,
                    level=event.level.value,
                    category=event.kind.value,
                )
            )

    def _record_presentation_changes(self, changes) -> None:
        for change in changes:
            if change.kind is PresentationChangeKind.UPDATE and change.cell is not None:
                self._layout_dirty_ids.add(change.cell.id)
            else:
                self._layout_structure_dirty = True

    def _is_root_transcript_event(self, event: RuntimeEvent) -> bool:
        """Keep child internals observable without publishing them as chat."""
        if isinstance(event.payload, SubagentJobChanged):
            return False
        if isinstance(event.payload, (ApprovalRequested, ApprovalResolved)):
            return True
        return (
            self.root_agent_id is None
            or event.agent_id is None
            or event.agent_id == self.root_agent_id
        )

    def append_user_command(self, text: str) -> None:
        with self._lock:
            self._notice_seq += 1
            cell_id = f"command:{self._notice_seq}"
            self.transcript.state.transcript.append(
                UserCell(id=cell_id, text=text, group_id=cell_id)
            )
            self._layout_structure_dirty = True
        self._invalidate()

    def restore_control_state(self, plan, progress, *, session_id: str | None) -> None:
        """Replace projections after an explicit session/new-context switch."""
        with self._lock:
            self.execution = ExecutionViewReducer(root_agent_id=self.root_agent_id)
            envelope = {
                "agent_id": self.root_agent_id,
                "session_id": session_id,
                "session_generation": plan.session_generation,
            }
            if plan.revision:
                self.execution.apply(
                    RuntimeEvent(
                        payload=PlanUpdated(
                            revision=plan.revision,
                            items=tuple(
                                {
                                    "step": item.step,
                                    "active_form": item.active_form,
                                    "status": item.status,
                                }
                                for item in plan.items
                            ),
                            explanation=plan.explanation,
                        ),
                        **envelope,
                    )
                )
            if progress.revision:
                self.execution.apply(
                    RuntimeEvent(
                        payload=ProgressReported(
                            revision=progress.revision,
                            phase=progress.phase,
                            summary=progress.summary,
                            next=progress.next,
                        ),
                        **envelope,
                    )
                )
        self._invalidate()

    def clear_transcript(self) -> None:
        """Clear only the visible canvas while preserving persisted session history."""
        with self._lock:
            policy = self.transcript.policy
            self.transcript = PresentationReducer(
                state=RuntimeViewState(
                    transcript=TranscriptModel(
                        max_cells=2_000,
                        max_text_chars=2_000_000,
                    )
                ),
                policy=policy,
            )
            self._cell_visual_cache.clear()
            self._transcript_layout_key = ()
            self._transcript_layout_source_key = None
            self._transcript_layout = VirtualTranscriptLayout(())
            self._layout_model_revision = -1
            self._layout_structure_dirty = True
            self._layout_dirty_ids.clear()
            self._placement_by_id.clear()
            self._flattened_layout = None
            self._transcript_rendered = FormattedText()
        self._invalidate()

    def append_restored_conversation(self, entries) -> None:
        """Replay a bounded human transcript without adding model history."""
        with self._lock:
            restored_group: str | None = None
            for index, entry in enumerate(entries):
                role = entry.get("role")
                content = str(entry.get("content") or "")
                cell_id = f"restored:{index}:{self._notice_seq}"
                if role == "user":
                    restored_group = f"restored-turn:{index}:{self._notice_seq}"
                    self.transcript.state.transcript.append(
                        UserCell(
                            id=cell_id,
                            text=content,
                            group_id=restored_group,
                        )
                    )
                elif role == "assistant":
                    self.transcript.state.transcript.append(
                        AssistantCell(
                            id=cell_id,
                            text=content,
                            complete=True,
                            group_id=restored_group or cell_id,
                        )
                    )
            self._notice_seq += 1
            self._layout_structure_dirty = True
        self._invalidate()

    def panel_lines(self, width: int) -> tuple[str, ...]:
        self._drain_pending_events()
        with self._lock:
            return execution_panel_lines(self.execution.state, width=width)

    def panel_view(self, *, now: float | None = None) -> ExecutionPanelView:
        self._drain_pending_events()
        with self._lock:
            return execution_panel_view(self.execution.state, now=now)

    def set_viewport_width(self, width: int) -> None:
        self._viewport_width = max(20, width)

    def has_animation_lease(self) -> bool:
        now = time.time()
        with self._lock:
            return any(
                agent.is_animating(now) or agent.operation_started_at is not None
                for agent in self.execution.state.agents.values()
            )

    def transcript_layout(self, width: int | None = None) -> VirtualTranscriptLayout:
        self._drain_pending_events()
        if width is not None:
            self.set_viewport_width(width)
        with self._lock:
            model = self.transcript.state.transcript
            source_key = (
                id(model),
                model.revision,
                self._viewport_width,
                self._theme_revision,
            )
            if source_key == self._transcript_layout_source_key:
                return self._transcript_layout
            cells = model.cells
            can_update_incrementally = (
                not self._layout_structure_dirty
                and self._layout_model_revision >= 0
                and bool(self._layout_dirty_ids)
                and self._transcript_layout_source_key is not None
                and self._transcript_layout_source_key[0] == id(model)
                and self._transcript_layout_source_key[2] == self._viewport_width
                and self._transcript_layout_source_key[3] == self._theme_revision
            )
        if can_update_incrementally:
            replacements: dict[str, VisualCell] = {}
            for cell_id in self._layout_dirty_ids:
                cell = model.get(cell_id)
                placement = self._placement_by_id.get(cell_id)
                if cell is None or placement is None:
                    self._layout_structure_dirty = True
                    break
                updated_placement = replace(placement, cell=cell)
                self._placement_by_id[cell_id] = updated_placement
                previous_visual = self._transcript_layout.cell(cell_id)
                updated_visual = self._visual_cell(updated_placement)
                replacements[cell_id] = updated_visual
                if (
                    previous_visual is not None
                    and previous_visual.key != updated_visual.key
                ):
                    self._cell_visual_cache.pop(previous_visual.key, None)
            if not self._layout_structure_dirty:
                self._transcript_layout = self._transcript_layout.with_replacements(
                    replacements
                )
                self._transcript_layout_source_key = source_key
                self._layout_model_revision = model.revision
                self._layout_dirty_ids.clear()
                self._flattened_layout = None
                return self._transcript_layout
        render_key = tuple(
            (
                cell.id,
                cell.revision,
                self._viewport_width,
                self._theme_revision,
            )
            for cell in cells
        )
        if render_key == self._transcript_layout_key:
            return self._transcript_layout
        placements = compose_transcript(cells)
        self._placement_by_id = {
            placement.cell.id: placement for placement in placements
        }
        visual_cells = [self._visual_cell(placement) for placement in placements]
        live_keys = {cell.key for cell in visual_cells}
        if len(self._cell_visual_cache) > max(50, len(live_keys) * 2):
            self._cell_visual_cache = {
                key: value
                for key, value in self._cell_visual_cache.items()
                if key in live_keys
            }
        self._transcript_layout_key = render_key
        self._transcript_layout_source_key = source_key
        self._transcript_layout = VirtualTranscriptLayout(tuple(visual_cells))
        self._layout_model_revision = model.revision
        self._layout_structure_dirty = False
        self._layout_dirty_ids.clear()
        self._flattened_layout = None
        return self._transcript_layout

    def _visual_cell(self, placement: TranscriptPlacement) -> VisualCell:
        cell = placement.cell
        decoration_revision = (
            cell.revision * 8
            + int(placement.begins_turn) * 4
            + int(placement.show_assistant_label) * 2
            + placement.blank_lines_after
        )
        key = (
            cell.id,
            decoration_revision,
            self._viewport_width,
            self._theme_revision,
        )
        lines = self._cell_visual_cache.get(key)
        if lines is None:
            lines = _fragments_to_visual_lines(
                _wrap_fragments(
                    _decorate_transcript_fragments(
                        placement,
                        _cell_fragments(
                            cell,
                            width=self._viewport_width,
                            markdown_renderer=self._markdown,
                        ),
                    ),
                    width=max(1, self._viewport_width),
                )
            )
            self._cell_visual_cache[key] = lines
        return VisualCell(key=key, lines=lines)

    def transcript_layout_rebased(
        self,
        width: int,
        scroll_line: int,
    ) -> tuple[VirtualTranscriptLayout, int]:
        """Render at width while preserving the prior top cell/local-line anchor."""
        previous = self._transcript_layout
        anchor = previous.anchor_at(scroll_line)
        current = self.transcript_layout(width)
        if current is previous:
            return current, scroll_line
        return current, current.line_for_anchor(anchor, fallback=scroll_line)

    def transcript_fragments(self) -> FormattedText:
        """Compatibility projection; the live Window resolves visual rows lazily."""
        layout = self.transcript_layout()
        if self._flattened_layout is layout:
            return self._transcript_rendered
        self._transcript_rendered = FormattedText(layout.flatten())
        self._flattened_layout = layout
        return self._transcript_rendered

