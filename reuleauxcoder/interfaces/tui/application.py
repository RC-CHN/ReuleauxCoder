"""Prompt Toolkit-owned production terminal UI.

The adapter intentionally contains no Plan or agent business state.  It renders
the shared presentation reducers and turns bottom-pane input into shared
interaction responses.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
import queue
import threading
import time
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.data_structures import Point
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame

from reuleauxcoder import __version__
from reuleauxcoder.app.commands import parse_command
from reuleauxcoder.app.commands.panels import (
    CommandPanelRegistry,
    PanelItem,
    PanelRefreshPolicy,
)
from reuleauxcoder.app.commands.specs import DuringTurnPolicy
from reuleauxcoder.app.runtime.session_state import (
    build_session_persistence_kwargs,
    build_session_runtime_state,
)
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
from reuleauxcoder.app.commands.view_models import (
    SessionResumeViewModel,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.tui.command_popup import (
    PopupEntry,
    build_popup_entries,
    filter_entries,
)
from reuleauxcoder.interfaces.tui.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.tui.selection_panel import SelectionPanel
from reuleauxcoder.interfaces.tui.virtual_transcript import (
    VirtualTranscriptControl,
    VirtualTranscriptLayout,
    VisualCell,
)
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.interactions import (
    ConfirmRequest,
    ReviewRequest,
)
from reuleauxcoder.interfaces.tui.interaction import (
    MiniTUIInteractor,
    interaction_lines as _interaction_lines,
)
from reuleauxcoder.interfaces.tui.formatting import (
    clip as _clip,
    fit_styled_row as _fit_styled_row,
    fragments_to_visual_lines as _fragments_to_visual_lines,
    wrap_fragments as _wrap_fragments,
    wrapped_row_count as _wrapped_row_count,
)
from reuleauxcoder.interfaces.tui.transcript import (
    cell_fragments as _cell_fragments,
    decorate_transcript_fragments as _decorate_transcript_fragments,
)
from reuleauxcoder.interfaces.tui.view_text import view_text as _view_text
from reuleauxcoder.presentation import (
    AssistantCell,
    ExecutionPanelView,
    ExecutionViewReducer,
    PresentationChangeKind,
    PresentationReducer,
    RuntimeViewState,
    TranscriptPlacement,
    TranscriptModel,
    UserCell,
    compose_transcript,
    execution_panel_lines,
    execution_panel_view,
)


MINI_TUI_STYLE = Style.from_dict(
    {
        "frame.label": "bold #071013 bg:#67e8f9",
        "frame.border": "#64748b",
        "panel.header": "bold #f8fafc bg:#334155",
        "panel.body": "#dbeafe",
        "panel.label": "bold #071013 bg:#67e8f9",
        "panel.label.secondary": "bold #e2e8f0 bg:#334155",
        "panel.label.need": "bold #071013 bg:#ffd75f",
        "panel.value": "#f8fafc",
        "panel.phase": "bold #b5ff72",
        "panel.live": "bold #67e8f9",
        "panel.detail": "#94a3b8",
        "user": "bold #ffffff bg:#5b4bc4",
        "user.label": "bold #ffffff bg:#6d5ce7",
        "assistant.label": "bold #071013 bg:#67e8f9",
        "turn.separator": "#475569",
        "assistant": "#f8fafc",
        "tool": "#67e8f9",
        "muted": "#94a3b8",
        "success": "#b5ff72",
        "warning": "#ffd75f",
        "error": "#ff8193",
        "diff.add": "#d8ffb0 bg:#17351f",
        "diff.del": "#ffd0d7 bg:#3a1720",
        "diff.header": "bold #67e8f9 bg:#102b33",
        "input": "#ffffff bg:#191827",
        "popup": "#8a86a8 bg:#1c1a2e",
        "popup.cmd": "bold #d8d4f0 bg:#1c1a2e",
        "popup.selected": "bold #ffffff bg:#5b4bc4",
        "interaction": "#fff7d6 bg:#332a12",
        "review.border": "bold #ffd75f",
        "review.approved": "bold #67e8f9",
        "review.denied": "bold #ff8193",
        "review.title.pending": "bold #071013 bg:#ffd75f",
        "review.title.approved": "bold #071013 bg:#67e8f9",
        "review.title.denied": "bold #071013 bg:#ff8193",
        "review.body": "#f8fafc",
        "scrollbar.background": "#29434a bg:#101a1e",
        "scrollbar.button": "#071013 bg:#67e8f9",
        "scrollbar.start": "underline",
        "scrollbar.end": "underline",
        "scrollbar.arrow": "bold #071013 bg:#67e8f9",
    }
)

# Terminal-native mouse ownership is intentional: when prompt_toolkit enables
# mouse tracking, ordinary drag selection never reaches Konsole/iTerm/etc.
# Keyboard transcript navigation remains available through PageUp/PageDown and
# Home/End while users retain native selection, copy, and paste behavior.
MINI_TUI_MOUSE_SUPPORT = False
ALTERNATE_SCROLL_ENABLE = "\x1b[?1007h"
ALTERNATE_SCROLL_DISABLE = "\x1b[?1007l"


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


class MiniTUIApplication:
    """Persistent top panel, scrollable transcript and modal bottom input."""

    def __init__(
        self,
        *,
        agent,
        config,
        ui_bus,
        ui_profile,
        action_registry,
        panel_registry: CommandPanelRegistry,
        interactor: MiniTUIInteractor,
        event_adapter: MiniTUIEventAdapter,
        current_session_id: str | None,
        sessions_dir: Path | None,
        session_exit_time: str | None,
        skills_service=None,
        startup_events: tuple[UIEvent, ...] = (),
        exit_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.ui_bus = ui_bus
        self.ui_profile = ui_profile
        self.action_registry = action_registry
        self.panel_registry = panel_registry
        self.interactor = interactor
        self.events = event_adapter
        self.current_session_id = current_session_id
        self.sessions_dir = sessions_dir
        self.session_exit_time = session_exit_time
        self.skills_service = skills_service
        self._exit_progress = exit_progress
        self.running = False
        self.cancelling = False
        self.exit_confirm = False
        self._exit_session_saved = False
        self._saved_session_id: str | None = None
        self._closed = False
        self._worker: threading.Thread | None = None
        self._deferred_commands: deque[str] = deque()
        self._deferred_commands_lock = threading.Lock()
        self._animation_stop = threading.Event()
        self._animation_thread: threading.Thread | None = None
        self._width = 100
        self._follow_transcript = True
        self._transcript_scroll = 0
        self._transcript_max_scroll = 0
        self._last_terminal_rows = 0
        self.session_header_expanded = True
        self.startup_lines = tuple(
            event.message.splitlines()[0]
            for event in startup_events
            if event.message.strip() and event.kind is not UIEventKind.MCP
        )[:6]

        history_path = (
            str(Path(config.history_file).expanduser())
            if getattr(config, "history_file", None)
            else str(Path.cwd() / ".rcoder" / "history")
        )
        self.input_buffer = Buffer(
            history=FileHistory(history_path),
            multiline=False,
            accept_handler=self._accept_buffer,
        )
        self.panel_control = FormattedTextControl(self._panel_text)
        self._popup_entries: tuple[PopupEntry, ...] = build_popup_entries(
            action_registry, ui_profile
        )
        self._popup_index = 0
        self._popup_last_text = ""
        self._popup_dismissed = False
        self._selection: SelectionPanel | None = None
        self._selection_stack: list[SelectionPanel] = []
        self.events.interactive_view_handler = self._open_interactive_view
        self.transcript_control = VirtualTranscriptControl(
            self.events.transcript_layout,
            self._transcript_cursor_position,
        )
        self.transcript_window = Window(
            self.transcript_control,
            wrap_lines=False,
            always_hide_cursor=True,
            get_vertical_scroll=lambda _window: self._transcript_scroll,
            right_margins=[
                ScrollbarMargin(
                    display_arrows=True,
                    up_arrow_symbol="▲",
                    down_arrow_symbol="▼",
                )
            ],
        )
        # Compatibility alias for the scroll state machine. Unlike
        # ScrollablePane, Window paints only its visible viewport and does not
        # allocate a transcript-height off-screen Screen on every frame.
        self.transcript_pane = self.transcript_window
        self.interaction_control = FormattedTextControl(self._interaction_text)
        self.popup_control = FormattedTextControl(self._popup_text)
        self.popup_window = Window(
            self.popup_control,
            height=self._popup_height,
            style="class:popup",
        )
        self.selection_control = FormattedTextControl(self._selection_text)
        self.selection_window = Window(
            self.selection_control,
            height=self._selection_height,
            style="class:popup",
        )
        self.input_window = Window(
            BufferControl(buffer=self.input_buffer),
            height=self._input_height,
            wrap_lines=True,
            style="class:input",
        )
        body = HSplit(
            [
                Frame(
                    Window(self.panel_control, height=self._panel_height),
                    title=lambda: f" FORGE · v{__version__} · F2 DETAILS ",
                    style="class:frame.border",
                ),
                self.transcript_window,
                Frame(
                    HSplit(
                        [
                            self.selection_window,
                            self.popup_window,
                            Window(
                                self.interaction_control,
                                height=self._interaction_height,
                                wrap_lines=True,
                            ),
                            self.input_window,
                        ]
                    ),
                    title=self._input_title,
                    style="class:frame.border",
                ),
            ]
        )
        self.application = Application(
            layout=Layout(body, focused_element=self.input_window),
            key_bindings=self._key_bindings(),
            full_screen=True,
            style=MINI_TUI_STYLE,
            mouse_support=MINI_TUI_MOUSE_SUPPORT,
            min_redraw_interval=1 / 30,
            max_render_postpone_time=0.05,
            before_render=self._before_render,
        )
        self.events.bind_invalidator(self.invalidate)
        self.interactor.bind_invalidator(self.invalidate)

    def run(self) -> None:
        self.agent.current_session_id = self.current_session_id
        self._animation_stop.clear()
        self._animation_thread = threading.Thread(
            target=self._animation_loop,
            name="rcoder-ui-animation",
            daemon=True,
        )
        self._animation_thread.start()
        try:
            self.application.run(
                pre_run=lambda: self._set_alternate_scroll(enabled=True)
            )
        finally:
            self._set_alternate_scroll(enabled=False)
            self._animation_stop.set()
            if self._animation_thread is not None:
                self._animation_thread.join(timeout=0.5)
            self._save_exit_session()

    @property
    def exit_session_saved(self) -> bool:
        """Whether this TUI exit produced a durable session snapshot."""
        return self._exit_session_saved

    @property
    def saved_session_id(self) -> str | None:
        return self._saved_session_id

    def _animation_loop(self) -> None:
        """Redraw leased activity without ever extending the runtime lease."""
        while not self._animation_stop.wait(0.1):
            if self.events.has_animation_lease():
                self.invalidate()

    def invalidate(self) -> None:
        if not self._closed:
            try:
                self.application.invalidate()
            except RuntimeError:
                pass

    def _accept_buffer(self, buffer: Buffer) -> bool:
        popup = self._popup_candidates()
        if popup:
            entry = popup[min(self._popup_index, len(popup) - 1)]
            if entry.completion != buffer.text.strip():
                # Adopt the highlighted candidate without submitting.
                self._popup_adopt()
                return True
        text = buffer.text.strip()
        active_request = self.interactor.active_request
        if active_request is not None:
            # Binary approvals borrow the input focus but not its contents.
            # The buffer may contain a chat draft from before the request
            # arrived; Enter accepts the advertised default without consuming
            # that draft, just like the dedicated Y / N bindings below.
            if isinstance(active_request, (ConfirmRequest, ReviewRequest)):
                self.interactor.submit("")
                return True
            # Choice and text interactions intentionally use the buffer as
            # their response field, so consume it only for those request kinds.
            buffer.reset()
            self.interactor.submit(text)
            return True
        buffer.reset()
        if not text:
            return True
        if self.running:
            self._submit_during_turn(text)
            self.invalidate()
            return True
        self.exit_confirm = False
        self.session_header_expanded = False
        # A stop request belongs to the turn that just finished.  Local slash
        # commands do not pass through Agent.chat(), so clear the stale flag at
        # the same idle-operation boundary before starting either kind of input.
        self.agent.clear_stop_request()
        self.running = True
        self.cancelling = False
        self._worker = threading.Thread(
            target=self._handle_input,
            args=(text,),
            name="rcoder-cli-turn",
            daemon=True,
        )
        self._worker.start()
        self.invalidate()
        return True

    def _submit_during_turn(self, text: str) -> None:
        """Route active-turn input without leaking slash commands to the model."""
        if not text.startswith("/"):
            # Queued steering hangs above the input lane as a preview and only
            # enters the transcript when the agent injects it (drain event).
            self.agent.submit_user_steering(text)
            return

        self.events.append_user_command(text)
        parsed = parse_command(
            text,
            ui_profile=self.ui_profile,
            action_registry=self.action_registry,
            current_session_id=self.current_session_id,
        )
        if (
            parsed is not None
            and parsed.action.during_turn is DuringTurnPolicy.DEFER_UNTIL_IDLE
        ):
            with self._deferred_commands_lock:
                self._deferred_commands.append(text)
            self.ui_bus.info(
                f"Queued command: {text}\n"
                "It will run when the current turn becomes idle. "
                "Press Ctrl+C to interrupt and apply it sooner.",
                kind=UIEventKind.COMMAND,
            )
            return

        thread = threading.Thread(
            target=self._handle_concurrent_command,
            args=(text,),
            name="rcoder-cli-command",
            daemon=True,
        )
        thread.start()

    def _handle_concurrent_command(self, user_input: str) -> None:
        """Execute an immediate local command alongside the active agent turn."""
        try:
            result = handle_command(
                user_input,
                self.agent,
                self.config,
                self.current_session_id,
                self.ui_bus,
                self.ui_profile,
                self.action_registry,
                self.sessions_dir,
                self.skills_service,
            )
            if result["action"] != "continue":
                self.ui_bus.warning(
                    f"Command '{user_input}' cannot change session control "
                    "while an agent turn is running.",
                    kind=UIEventKind.COMMAND,
                )
        except Exception as error:
            self.ui_bus.error(
                f"Command failed: {error}",
                kind=UIEventKind.COMMAND,
            )
        finally:
            self.invalidate()

    def _handle_input(self, user_input: str, record_command: bool = True) -> None:
        drain_deferred = True
        try:
            previous_session_id = self.current_session_id
            previous_generation = self.agent.session_generation
            if record_command and user_input.startswith("/"):
                self.events.append_user_command(user_input)
            result = handle_command(
                user_input,
                self.agent,
                self.config,
                self.current_session_id,
                self.ui_bus,
                self.ui_profile,
                self.action_registry,
                self.sessions_dir,
                self.skills_service,
            )
            self.current_session_id = result["session_id"]
            self.agent.current_session_id = self.current_session_id
            session_changed = (
                self.current_session_id != previous_session_id
                or self.agent.session_generation != previous_generation
            )
            if (
                result.get("action_id") == "sessions.new"
                and result["action"] == "continue"
                and session_changed
            ):
                self.events.clear_transcript()
                self._follow_transcript = True
                self._transcript_scroll = 0
                self.transcript_pane.vertical_scroll = 0
            if result["action"] == "continue" and session_changed:
                self.events.restore_control_state(
                    self.agent.plan_controller.state,
                    self.agent.plan_controller.progress,
                    session_id=self.current_session_id,
                )
            if result["action"] == "exit":
                drain_deferred = False
                self._clear_deferred_commands()
                self._exit_session_saved = (
                    result.get("action_id") == "system.exit"
                    and self.config.session_auto_save
                    and bool(self.agent.messages)
                )
                if self._exit_session_saved:
                    self._saved_session_id = (
                        result.get("session_id") or self.current_session_id
                    )
                self.application.exit()
                return
            if result["action"] == "continue":
                return
            chat_input = user_input
            if self.session_exit_time is not None:
                now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
                chat_input = (
                    f"[SESSION_RESUME] User returned to the session at {now} "
                    f"(last left at {self.session_exit_time}).\n\n{user_input}"
                )
                self.session_exit_time = None
            self.agent.chat(chat_input)
        except KeyboardInterrupt:
            self.agent.request_stop()
            self.ui_bus.warning("Interrupted.")
        except Exception as error:
            diagnostic = getattr(error, "llm_diagnostic_path", None)
            suffix = f"\nDiagnostic saved to: {diagnostic}" if diagnostic else ""
            self.ui_bus.error(f"Error: {error}{suffix}")
        finally:
            was_cancelling = self.cancelling
            self.cancelling = False
            if was_cancelling:
                self.ui_bus.info(
                    "Current turn cancelled.",
                    kind=UIEventKind.AGENT,
                )
            started_deferred = (
                drain_deferred and self._start_next_deferred_command()
            )
            if not started_deferred:
                self.running = False
            self.invalidate()

    def _start_next_deferred_command(self) -> bool:
        """Start the next queued local command once the active worker is idle."""
        with self._deferred_commands_lock:
            if self._closed or not self._deferred_commands:
                return False
            command = self._deferred_commands.popleft()

        self.agent.clear_stop_request()
        self.ui_bus.info(
            f"Applying queued command now: {command}",
            kind=UIEventKind.COMMAND,
        )
        self.running = True
        self._worker = threading.Thread(
            target=self._handle_input,
            args=(command, False),
            name="rcoder-cli-command",
            daemon=True,
        )
        self._worker.start()
        return True

    def _queued_commands(self) -> tuple[str, ...]:
        lock = getattr(self, "_deferred_commands_lock", None)
        commands = getattr(self, "_deferred_commands", ())
        if lock is None:
            return tuple(commands)
        with lock:
            return tuple(commands)

    def _clear_deferred_commands(self) -> None:
        lock = getattr(self, "_deferred_commands_lock", None)
        commands = getattr(self, "_deferred_commands", None)
        if commands is None:
            return
        if lock is None:
            commands.clear()
            return
        with lock:
            commands.clear()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        transcript_arrow_scroll = Condition(self._should_route_arrows_to_transcript)
        binary_interaction_active = Condition(
            lambda: isinstance(
                self.interactor.active_request,
                (ConfirmRequest, ReviewRequest),
            )
        )

        @bindings.add("c-c")
        def _ctrl_c(event) -> None:
            if self.interactor.cancel_active():
                # Cancelling a modal approval must not discard the chat draft
                # that was present before the request took over the input lane.
                return
            if self.input_buffer.text:
                self.input_buffer.reset()
                self.exit_confirm = False
                return
            if self.running:
                if self.cancelling:
                    self._prepare_forced_exit("forced CLI exit during active turn")
                    self._closed = True
                    event.app.exit()
                else:
                    self.cancelling = True
                    self.agent.request_stop()
                    queued_steering = self._queued_steering()
                    queued_commands = self._queued_commands()
                    if queued_commands and queued_steering:
                        message = (
                            "Cancelling the current turn. Queued commands will run "
                            "next; queued steers will be discarded."
                        )
                    elif queued_commands:
                        message = (
                            "Cancelling the current turn. Queued commands will run next."
                        )
                    elif queued_steering:
                        message = (
                            "Cancelling the current turn. Queued steers will be discarded."
                        )
                    else:
                        message = "Cancelling the current turn…"
                    self.ui_bus.warning(message)
                return
            if self.exit_confirm:
                self._closed = True
                event.app.exit()
            else:
                self.exit_confirm = True
                self.invalidate()

        @bindings.add("escape")
        def _escape(event) -> None:  # noqa: ARG001
            self.exit_confirm = False
            self.invalidate()

        @bindings.add("pageup")
        def _page_up(event) -> None:  # noqa: ARG001
            self._scroll_transcript(-self._transcript_page_size())

        @bindings.add("pagedown")
        def _page_down(event) -> None:  # noqa: ARG001
            self._scroll_transcript(self._transcript_page_size())

        @bindings.add("up", filter=transcript_arrow_scroll)
        def _alternate_scroll_up(event) -> None:  # noqa: ARG001
            self._scroll_transcript(-3)

        @bindings.add("down", filter=transcript_arrow_scroll)
        def _alternate_scroll_down(event) -> None:  # noqa: ARG001
            self._scroll_transcript(3)

        @bindings.add("home")
        def _history_start(event) -> None:  # noqa: ARG001
            self._follow_transcript = False
            self._transcript_scroll = 0
            self.transcript_pane.vertical_scroll = 0
            self.invalidate()

        @bindings.add("end")
        def _history_end(event) -> None:  # noqa: ARG001
            self._follow_transcript = True
            self.invalidate()

        selection_active = Condition(lambda: self._selection is not None)

        @bindings.add("up", filter=selection_active)
        def _selection_up(event) -> None:  # noqa: ARG001
            if self._selection is not None:
                visible = len(self._selection_visible_items())
                if visible:
                    self._selection.index = (self._selection.index - 1) % visible
                self.invalidate()

        @bindings.add("down", filter=selection_active)
        def _selection_down(event) -> None:  # noqa: ARG001
            if self._selection is not None:
                visible = len(self._selection_visible_items())
                if visible:
                    self._selection.index = (self._selection.index + 1) % visible
                self.invalidate()

        @bindings.add("enter", filter=selection_active)
        def _selection_enter(event) -> None:  # noqa: ARG001
            self._selection_confirm()

        @bindings.add("escape", filter=selection_active)
        def _selection_escape(event) -> None:  # noqa: ARG001
            self._selection_close()

        popup_visible = Condition(lambda: bool(self._popup_candidates()))

        @bindings.add("up", filter=popup_visible)
        def _popup_up(event) -> None:  # noqa: ARG001
            candidates = self._popup_candidates()
            if candidates:
                self._popup_index = (self._popup_index - 1) % len(candidates)
                self.invalidate()

        @bindings.add("down", filter=popup_visible)
        def _popup_down(event) -> None:  # noqa: ARG001
            candidates = self._popup_candidates()
            if candidates:
                self._popup_index = (self._popup_index + 1) % len(candidates)
                self.invalidate()

        @bindings.add("tab", filter=popup_visible)
        def _popup_tab(event) -> None:  # noqa: ARG001
            self._popup_adopt()

        @bindings.add("escape", filter=popup_visible)
        def _popup_escape(event) -> None:  # noqa: ARG001
            self._popup_dismissed = True
            self.invalidate()

        @bindings.add("y", filter=binary_interaction_active)
        def _interaction_yes(event) -> None:  # noqa: ARG001
            self.interactor.submit("y")
            self.invalidate()

        @bindings.add("n", filter=binary_interaction_active)
        def _interaction_no(event) -> None:  # noqa: ARG001
            self.interactor.submit("n")
            self.invalidate()

        @bindings.add("f2")
        def _toggle_header(event) -> None:  # noqa: ARG001
            self.session_header_expanded = not self.session_header_expanded
            self.invalidate()

        return bindings

    def _panel_text(self) -> FormattedText:
        try:
            self._width = self.application.output.get_size().columns - 4
        except Exception:
            pass
        self.events.set_viewport_width(max(20, self._width - 1))
        return FormattedText(
            fragment for row in self._panel_rows() for fragment in (*row, ("", "\n"))
        )

    def _panel_height(self) -> int:
        return len(self._panel_rows())

    def _panel_lines(self) -> tuple[str, ...]:
        return tuple(
            "".join(text for _style, text in row) for row in self._panel_rows()
        )

    def _panel_rows(self) -> tuple[tuple[tuple[str, str], ...], ...]:
        details = (
            # MODEL lives in the always-visible right-side context tail.
            f"ROOT {Path.cwd()}",
            f"SESSION {self.current_session_id or 'new'}",
            self._mcp_panel_detail(),
            *self.startup_lines,
        )
        rows = _execution_panel_rows(
            self.events.panel_view(),
            width=max(20, self._width),
            expanded=self.session_header_expanded,
            details=details,
        )
        tail = self._context_tail()
        if tail and rows:
            width = max(20, self._width)
            first = rows[0]
            used = sum(get_cwidth(text) for _style, text in first)
            tail_width = sum(get_cwidth(text) for _style, text in tail)
            padding = " " * max(1, width - used - tail_width)
            rows = (_fit_styled_row([*first, ("", padding), *tail], width), *rows[1:])
        return rows

    def _mcp_panel_detail(self) -> str:
        """Return a live MCP summary for the session header."""
        servers = tuple(getattr(self.config, "mcp_servers", ()) or ())
        enabled = sum(1 for server in servers if getattr(server, "enabled", True))
        manager = getattr(self.agent, "mcp_manager", None)
        state = str(getattr(manager, "initial_state", "ready"))
        tools = int(getattr(manager, "available_tool_count", 0) or 0)
        if state == "connecting":
            return f"MCP connecting · {enabled} enabled · {tools} tools"
        return f"MCP {enabled} enabled · {tools} tools"

    def _context_tail(self) -> tuple[tuple[str, str], ...]:
        """Right-side summary: runtime model plus context capacity bar."""
        agent = self.agent
        try:
            revision = getattr(agent, "_context_revision", 0)
            cached = getattr(self, "_context_tail_cache", None)
            if cached is not None and cached[0] == revision:
                return cached[1]
            current = agent.context.predict_request_tokens(agent.messages)
            limit = agent.context.request_input_limit
        except Exception:
            return ()
        model = getattr(getattr(agent, "llm", None), "model", None) or self.config.model
        fragments: list[tuple[str, str]] = [("class:panel.label.secondary", f" {model} ")]
        if limit:
            ratio = max(0.0, min(1.0, current / limit))
            filled = round(ratio * 8)
            bar = "█" * filled + "·" * (8 - filled)
            style = (
                "class:success"
                if ratio < 0.6
                else ("class:warning" if ratio < 0.8 else "class:error")
            )
            fragments.append((style, bar))
            fragments.append(("class:panel.value", f" {ratio * 100:.0f}%"))
        result = tuple(fragments)
        self._context_tail_cache = (revision, result)
        return result

    def _input_height(self) -> int:
        """Grow the single-line input visually as wrapped rows (capped)."""
        # Hide the input lane while an approval/review is pending so the draft
        # buffer is preserved and single-key Y / N bindings take over. The
        # session picker keeps it visible: the buffer doubles as its filter.
        if self.interactor.active_request is not None:
            return 0
        if self._selection is not None and self._selection.view_type not in (
            "sessions",
            "subagent_jobs",
        ):
            return 0
        try:
            columns = self.application.output.get_size().columns
        except Exception:
            columns = self._width
        content_width = max(20, columns - 4)
        return _wrapped_row_count(self.input_buffer.text, content_width, cap=8)

    def _queued_steering(self) -> tuple[str, ...]:
        preview = getattr(getattr(self, "agent", None), "pending_user_steering", None)
        if not callable(preview):
            return ()
        result = preview()
        if not isinstance(result, (tuple, list)):
            return ()
        return tuple(str(item) for item in result)

    def _popup_candidates(self) -> tuple[PopupEntry, ...]:
        if self.interactor.active_request is not None or self._selection is not None:
            return ()
        text = self.input_buffer.text
        if text != self._popup_last_text:
            self._popup_last_text = text
            self._popup_index = 0
            self._popup_dismissed = False
        if self._popup_dismissed:
            return ()
        return filter_entries(self._popup_entries, text)

    def _popup_height(self) -> int:
        return min(8, len(self._popup_candidates()))

    def _popup_adopt(self) -> None:
        candidates = self._popup_candidates()
        if not candidates:
            return
        entry = candidates[min(self._popup_index, len(candidates) - 1)]
        text = entry.completion + (" " if entry.has_arg else "")
        self.input_buffer.text = text
        self.input_buffer.cursor_position = len(text)
        self.invalidate()

    def _popup_text(self) -> FormattedText:
        candidates = self._popup_candidates()
        if not candidates:
            return FormattedText([])
        limit = 8
        index = min(self._popup_index, len(candidates) - 1)
        start = max(0, min(index - limit // 2, max(0, len(candidates) - limit)))
        fragments: list[tuple[str, str]] = []
        for offset, entry in enumerate(candidates[start : start + limit]):
            i = start + offset
            marker = "›" if i == index else " "
            cmd = f" {marker} {entry.completion}"
            pad = " " * max(1, 24 - len(cmd))
            if i == index:
                fragments.append(
                    ("class:popup.selected", cmd + pad + entry.description + "\n")
                )
            else:
                fragments.append(("class:popup.cmd", cmd))
                fragments.append(("class:popup", pad + entry.description + "\n"))
        return FormattedText(fragments)

    def _open_interactive_view(self, payload) -> bool:
        """Claim a command-owned view as a modal panel or absorb its refresh."""
        spec = self.panel_registry.get(payload.view_type)
        if spec is None:
            return False
        definition = spec.build_for(payload.view_model, payload.title)
        if definition is None:
            return False

        is_refresh = payload.action == "refresh" or not payload.focus
        if is_refresh:
            if (
                spec.refresh is PanelRefreshPolicy.UPDATE
                and self._selection is not None
                and self._selection.view_type == definition.view_type
            ):
                self._selection.refresh(definition)
                self.invalidate()
            return True

        self._selection = SelectionPanel.from_definition(definition)
        self._selection_stack = []
        self.invalidate()
        return True

    def _selection_visible_items(self) -> tuple[PanelItem, ...]:
        """Return panel rows after applying an optional live text filter."""
        panel = self._selection
        if panel is None:
            return ()
        definition = panel.definition
        if definition is None or not definition.filterable:
            return panel.items
        needle = self.input_buffer.text.strip().lower()
        if not needle:
            return panel.items
        return tuple(
            item
            for item in panel.items
            if needle in f"{item.label} {item.description}".lower()
        )

    def _selection_height(self) -> int:
        if self._selection is None:
            return 0
        return min(9, len(self._selection_visible_items()) + 1)

    def _selection_close(self) -> None:
        if self._selection_stack:
            self._selection = self._selection_stack.pop()
        else:
            self._selection = None
        self.invalidate()

    def _selection_confirm(self) -> None:
        panel = self._selection
        if panel is None or panel.definition is None:
            return
        items = self._selection_visible_items()
        if not items:
            return
        selected = items[min(panel.index, len(items) - 1)]
        child = panel.definition.child_for(selected.label)
        if child is not None:
            self._selection_stack.append(panel)
            self._selection = SelectionPanel.from_definition(child)
            self.invalidate()
            return
        command = selected.command
        if not command:
            # Placeholder/hint rows carry no command.
            return
        if not panel.definition.keep_open_on_submit:
            if panel.definition.return_to_parent_on_submit:
                self._selection = (
                    self._selection_stack.pop() if self._selection_stack else None
                )
            else:
                self._selection = None
                self._selection_stack = []
        self.input_buffer.text = command
        self.input_buffer.cursor_position = len(command)
        self._accept_buffer(self.input_buffer)

    def _selection_text(self) -> FormattedText:
        panel = self._selection
        if panel is None:
            return FormattedText([])
        items = self._selection_visible_items()
        definition = panel.definition
        hint = (
            " · type to filter"
            if definition is not None and definition.filterable
            else ""
        )
        fragments: list[tuple[str, str]] = [
            ("class:popup.cmd", f" {panel.title} "),
            ("class:popup", f"· Enter select{hint} · Esc close\n"),
        ]
        if not items:
            fragments.append(("class:popup", "  (no matches)\n"))
            return FormattedText(fragments)
        limit = 8
        index = min(panel.index, max(0, len(items) - 1))
        start = max(0, min(index - limit // 2, max(0, len(items) - limit)))
        for offset, item in enumerate(items[start : start + limit]):
            i = start + offset
            marker = "›" if i == index else " "
            current = " (current)" if item.current else ""
            row = f" {marker} {item.label}{current}"
            pad = " " * max(1, 24 - len(row))
            if i == index:
                fragments.append(
                    ("class:popup.selected", row + pad + item.description + "\n")
                )
            else:
                fragments.append(("class:popup.cmd", row))
                fragments.append(("class:popup", pad + item.description + "\n"))
        return FormattedText(fragments)

    def _interaction_height(self) -> int:
        request = self.interactor.active_request
        if request is None:
            queued_count = len(self._queued_steering()) + len(self._queued_commands())
            if not queued_count:
                return 1
            return 1 + min(3, queued_count) + int(queued_count > 3)
        return min(
            8,
            max(
                2,
                len(
                    _interaction_lines(
                        request,
                    )
                ),
            ),
        )

    def _interaction_text(self) -> FormattedText:
        request = self.interactor.active_request
        if request is not None:
            return FormattedText(
                [
                    ("class:interaction", line + "\n")
                    for line in _interaction_lines(
                        request,
                    )
                ]
            )
        if self.exit_confirm:
            return FormattedText(
                [
                    (
                        "class:warning",
                        "Press Ctrl+C again to exit; Esc keeps the session.\n",
                    )
                ]
            )
        if self.cancelling:
            return FormattedText(
                [("class:warning", "Cancelling the current turn…\n")]
            )
        if self.running:
            queued_steering = self._queued_steering()
            queued_commands = self._queued_commands()
            pending = [
                ("class:user", f" ↳ steer next: {_clip(text, 48)}\n")
                for text in queued_steering
            ]
            pending.extend(
                ("class:command", f" ⌛ when idle: {_clip(text, 48)}\n")
                for text in queued_commands
            )
            lines = pending[:3]
            if len(pending) > 3:
                lines.append(
                    ("class:muted", f" … {len(pending) - 3} more queued\n")
                )
            if queued_commands and queued_steering:
                hint = "Ctrl+C cancels: commands run next; steers are discarded\n"
            elif queued_commands:
                hint = "Ctrl+C cancels the turn and runs queued commands next\n"
            elif queued_steering:
                hint = "Ctrl+C cancels the turn and discards queued steers\n"
            else:
                hint = "Agent running · Enter queues a steer · Ctrl+C cancels\n"
            lines.append(("class:muted", hint))
            return FormattedText(lines)
        return FormattedText(
            [
                (
                    "class:muted",
                    "/help · wheel/PageUp scroll · drag to select/copy\n",
                )
            ]
        )

    def _input_title(self) -> str:
        return " REVIEW " if self.interactor.active_request else " YOU "

    def _should_route_arrows_to_transcript(self) -> bool:
        return self.interactor.active_request is not None or not self.input_buffer.text

    def _set_alternate_scroll(self, *, enabled: bool) -> None:
        """Let the terminal translate wheel motion to Up/Down without mouse capture."""
        try:
            self.application.output.write_raw(
                ALTERNATE_SCROLL_ENABLE if enabled else ALTERNATE_SCROLL_DISABLE
            )
            self.application.output.flush()
        except (AttributeError, OSError, RuntimeError):
            # Minimal/dumb outputs can omit raw terminal control support.
            return

    def _before_render(self, _app) -> None:
        """Clamp scrolling and follow new output only while tail-follow is on."""
        try:
            size = self.application.output.get_size()
            content_width = max(20, size.columns - 1)
            layout, rebased_scroll = self.events.transcript_layout_rebased(
                content_width,
                self._transcript_scroll,
            )
            content_height = layout.line_count
            resized = size.rows != self._last_terminal_rows
            viewport = max(
                1,
                (
                    size.rows - self._panel_height() - self._interaction_height() - 6
                    if resized or not self.transcript_control.last_height
                    else self.transcript_control.last_height
                ),
            )
            self._last_terminal_rows = size.rows
            maximum = max(0, content_height - viewport)
            self._transcript_max_scroll = maximum
            if self._follow_transcript:
                self._transcript_scroll = maximum
            else:
                self._transcript_scroll = min(rebased_scroll, maximum)
                if self._transcript_scroll >= maximum:
                    self._follow_transcript = True
            self.transcript_pane.vertical_scroll = self._transcript_scroll
        except Exception:
            # Rendering must stay available on minimal/dumb terminal outputs.
            return

    def _transcript_page_size(self) -> int:
        try:
            rows = self.application.output.get_size().rows
        except Exception:
            rows = 24
        return max(3, rows // 2)

    def _transcript_cursor_position(self) -> Point:
        return Point(x=0, y=max(0, self._transcript_scroll))

    def _scroll_transcript(self, delta: int) -> None:
        target = max(
            0,
            min(
                self._transcript_max_scroll,
                self._transcript_scroll + delta,
            ),
        )
        self._transcript_scroll = target
        self.transcript_pane.vertical_scroll = target
        # Scrolling up opts out of tail-follow. Returning to the current bottom
        # opts back in, so subsequent streaming chunks remain visible.
        self._follow_transcript = target >= self._transcript_max_scroll
        self.invalidate()

    def _save_exit_session(self) -> None:
        self._closed = True
        self._prepare_forced_exit("CLI session closed")
        if (
            getattr(self, "_exit_session_saved", False)
            or not self.agent.messages
            or not self.config.session_auto_save
        ):
            return
        progress = getattr(self, "_exit_progress", None)
        operation_id = f"session-save:{self.current_session_id or 'new'}"
        started = time.monotonic()
        if progress is not None:
            progress("Saving session snapshot...")
        ui_bus = getattr(self, "ui_bus", None)
        if ui_bus is not None:
            ui_bus.emit_operation_phase(
                operation_id=operation_id,
                operation="shutdown",
                phase="save_session",
                started_at=time.time(),
                cancelable=False,
                agent_id=getattr(self.agent, "agent_id", None),
                session_generation=getattr(
                    self.agent, "session_generation", None
                ),
                session_id=self.current_session_id,
            )
        store = SessionStore(self.sessions_dir)
        set_progress = getattr(store, "set_progress_callback", None)
        if callable(set_progress):
            set_progress(progress)
        try:
            sid = store.save(
                self.agent.messages,
                self.config.model,
                self.current_session_id,
                is_exit=True,
                total_prompt_tokens=self.agent.state.total_prompt_tokens,
                total_completion_tokens=self.agent.state.total_completion_tokens,
                active_mode=getattr(self.agent, "active_mode", None),
                runtime_state=build_session_runtime_state(self.config, self.agent),
                incremental=True,
                events_already_persisted=True,
                **build_session_persistence_kwargs(self.agent),
            )
        except Exception as error:
            elapsed = time.monotonic() - started
            if ui_bus is not None:
                ui_bus.emit_operation_phase(
                    operation_id=operation_id,
                    operation="shutdown",
                    phase="save_session",
                    status="failed",
                    detail=str(error)[:160] or type(error).__name__,
                    elapsed_ms=int(elapsed * 1000),
                    error_type=type(error).__name__,
                    agent_id=getattr(self.agent, "agent_id", None),
                    session_generation=getattr(
                        self.agent, "session_generation", None
                    ),
                    session_id=self.current_session_id,
                )
            if progress is not None:
                progress(
                    f"Session snapshot failed after {elapsed:.1f}s: "
                    f"{type(error).__name__}: {error}"
                )
            raise
        self._exit_session_saved = True
        self._saved_session_id = sid
        self.agent.lifecycle.session_saved(sid)
        elapsed = time.monotonic() - started
        if ui_bus is not None:
            ui_bus.emit_operation_phase(
                operation_id=operation_id,
                operation="shutdown",
                phase="save_session",
                status="completed",
                elapsed_ms=int(elapsed * 1000),
                agent_id=getattr(self.agent, "agent_id", None),
                session_generation=getattr(
                    self.agent, "session_generation", None
                ),
                session_id=sid,
            )
        if progress is not None:
            progress(f"Session snapshot committed in {elapsed:.1f}s.")

    def _prepare_forced_exit(self, reason: str) -> None:
        self._clear_deferred_commands()
        self.agent.request_stop()
        self.interactor.cancel_active(reason)
        reconcile = getattr(self.agent, "reconcile_pending_tool_calls", None)
        if callable(reconcile):
            reconcile(reason)


def _execution_panel_rows(
    view: ExecutionPanelView,
    *,
    width: int,
    expanded: bool,
    details: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Render a semantic panel snapshot without leaking layout into reducers."""
    width = max(20, width)
    plan_count = f"{view.plan_completed}/{view.plan_total}" if view.plan_total else "—"
    live = "LIVE" if view.is_live else "IDLE"

    if width < 60:
        summary = [
            ("class:panel.label", " RUN "),
            ("class:panel.phase", f" {view.phase} "),
            ("class:panel.label.secondary", " P "),
            ("class:panel.value", f" {plan_count} "),
            ("class:panel.label.secondary", " A "),
            ("class:panel.value", f" {len(view.subagents)}"),
        ]
        final = _compact_panel_tail(view)
        rows = [
            _fit_styled_row(summary, width),
            _fit_styled_row(
                _labeled_panel_row(
                    "PLAN", f"{'●' if view.plan_total else '○'} {view.active_plan}"
                ),
                width,
            ),
            _fit_styled_row(final, width),
        ]
    else:
        summary = [
            ("class:panel.label", " RUN "),
            ("class:panel.phase", f" {view.phase} "),
            ("class:panel.label.secondary", " PLAN "),
            ("class:panel.value", f" {plan_count} "),
            ("class:panel.label.secondary", " AGENTS "),
            ("class:panel.value", f" {len(view.subagents)} "),
            ("class:panel.live", f"● {live}"),
        ]
        if view.attention:
            summary.extend(
                [
                    ("class:panel.label.need", " NEED "),
                    ("class:warning", f" {len(view.attention)}"),
                ]
            )
        rows = [
            _fit_styled_row(summary, width),
            _fit_styled_row(
                _labeled_panel_row(
                    "PLAN", f"{'●' if view.plan_total else '○'} {view.active_plan}"
                ),
                width,
            ),
            _fit_styled_row(
                _labeled_panel_row(
                    "MAIN",
                    f"{view.main.marker} {view.main.activity or 'ready'}",
                ),
                width,
            ),
            _fit_styled_row(_compact_panel_tail(view), width),
        ]

    if not expanded:
        return tuple(rows)

    expanded_rows: list[tuple[tuple[str, str], ...]] = list(rows)
    for detail in details[:3]:
        label, _, value = detail.partition(" ")
        expanded_rows.append(
            _fit_styled_row(_labeled_panel_row(label, value, secondary=True), width)
        )
    for item in view.plan:
        marker = {
            "completed": "✓",
            "in_progress": "●",
            "pending": "○",
        }.get(item.status, "○")
        label = item.active_form if item.status == "in_progress" else item.step
        expanded_rows.append(
            _fit_styled_row(
                _labeled_panel_row("PLAN", f"{marker} {label}", secondary=True),
                width,
            )
        )
    for agent in view.subagents:
        expanded_rows.append(
            _fit_styled_row(
                _labeled_panel_row("SUB", _panel_agent_text(agent), secondary=True),
                width,
            )
        )
    for detail in details[3:]:
        expanded_rows.append(
            _fit_styled_row([("class:panel.detail", f"  {detail}")], width)
        )
    return tuple(expanded_rows[:12])


def _compact_panel_tail(view: ExecutionPanelView) -> list[tuple[str, str]]:
    if view.attention:
        row = _labeled_panel_row(
            "NEED",
            f"! {view.attention[0].title}",
            need=True,
        )
        if view.subagents:
            row.extend(
                [
                    ("class:panel.label.secondary", " SUB "),
                    ("class:panel.value", f" {_panel_agent_text(view.subagents[0])}"),
                ]
            )
        return row
    if view.subagents:
        return _labeled_panel_row("SUB", _panel_agent_text(view.subagents[0]))
    next_step = view.progress_next or view.progress_summary or "ready"
    return _labeled_panel_row("NEXT", next_step, secondary=True)


def _panel_agent_text(agent) -> str:
    task = agent.task or "working"
    activity = f" · {agent.activity}" if agent.activity else ""
    budget = f" · {agent.budget}" if agent.budget else ""
    return f"{agent.marker} {agent.label} · {task}{activity}{budget}"


def _labeled_panel_row(
    label: str,
    value: str,
    *,
    secondary: bool = False,
    need: bool = False,
) -> list[tuple[str, str]]:
    label_style = (
        "class:panel.label.need"
        if need
        else "class:panel.label.secondary"
        if secondary
        else "class:panel.label"
    )
    value_style = "class:warning" if need else "class:panel.value"
    return [(label_style, f" {label:<5} "), (value_style, f" {value}")]
