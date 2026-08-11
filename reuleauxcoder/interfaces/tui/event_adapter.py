"""Thread-safe event projection and virtual transcript state for the TUI."""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Callable

from prompt_toolkit.formatted_text import FormattedText

from reuleauxcoder.app.commands.view_models import SessionResumeViewModel
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
    SubagentJobChanged,
)
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeIssueSink,
    RuntimeEventPayload,
    UIEvent,
    ViewEventPayload,
    deliver_runtime_issue,
)
from reuleauxcoder.interfaces.interactions import ReviewRequest
from reuleauxcoder.interfaces.tui.formatting import (
    fragments_to_visual_lines as _fragments_to_visual_lines,
    wrap_fragments as _wrap_fragments,
)
from reuleauxcoder.interfaces.tui.event_queue import (
    DEFAULT_CONTROL_RESERVE,
    DEFAULT_EVENT_QUEUE_CAPACITY,
    DEFAULT_MAX_COALESCED_CHARS,
    DEFAULT_MUST_DELIVER_TIMEOUT_SECONDS,
    BoundedUIEventQueue,
    EventPutFailureReason,
    EventPutResult,
    EventQueueStats,
)
from reuleauxcoder.interfaces.tui.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.tui.transcript import (
    cell_fragments as _cell_fragments,
    decorate_transcript_fragments as _decorate_transcript_fragments,
)
from reuleauxcoder.interfaces.tui.transcript_cache import (
    TranscriptCacheStats,
    TranscriptLayoutPrewarmer,
    TranscriptPrewarmObservation,
    TranscriptPrewarmResult,
    visual_cell_key,
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


_QUEUE_SAMPLE_INTERVAL_SECONDS = 1.0
_MAX_PENDING_INCIDENT_KEYS = 8
_MAX_INCIDENT_COUNT = 1_000_000


@dataclass(frozen=True, slots=True)
class _UIIncident:
    kind: str
    detail: str
    error_type: str | None = None


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name and len(name) <= 64 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "Exception"


def _incident_message(incident: _UIIncident, count: int) -> str:
    if incident.kind == "delivery":
        message = (
            f"UI delivery failed ({incident.detail}); event projection was rejected."
        )
    elif incident.kind == "projection":
        message = (
            f"UI projection failed in {incident.detail} "
            f"({incident.error_type or 'Exception'}); event processing continued."
        )
    elif incident.kind == "invalidate":
        message = (
            f"UI refresh request failed ({incident.error_type or 'Exception'}); "
            "incident remains visible."
        )
    elif incident.kind == "monitor":
        message = (
            f"UI performance monitoring failed ({incident.error_type or 'Exception'})."
        )
    elif incident.kind == "observer":
        message = (
            f"UI incident reporting failed ({incident.error_type or 'Exception'})."
        )
    else:
        message = "Additional UI incidents were suppressed."
    return f"{message} [count={max(1, count)}]"


def _incident_runtime_facts(incident: _UIIncident) -> tuple[str, str, str]:
    if incident.kind == "delivery":
        return "ui_delivery", "EventRejected", incident.detail
    if incident.kind == "projection":
        return "ui_projection", incident.error_type or "Exception", incident.detail
    if incident.kind == "invalidate":
        return "ui_refresh", incident.error_type or "Exception", incident.detail
    if incident.kind == "monitor":
        return "ui_monitor", incident.error_type or "Exception", incident.detail
    return "ui", incident.error_type or "Exception", "incident"


def _event_generation(
    event: UIEvent,
    *,
    owner_agent_id: str | None = None,
) -> int | None:
    payload = event.payload
    if not isinstance(payload, RuntimeEventPayload):
        return None
    runtime = payload.event
    generation_owner_agent_id = payload.generation_owner_agent_id or runtime.agent_id
    if owner_agent_id is not None and generation_owner_agent_id not in {
        None,
        owner_agent_id,
    }:
        return None
    generation = runtime.session_generation
    if not isinstance(generation, int) or isinstance(generation, bool):
        return None
    return generation


class MiniTUIEventAdapter:
    """Thread-safe, source-backed event projection for the mini-TUI."""

    def __init__(
        self,
        *,
        root_agent_id: str | None = None,
        session_generation: int | None = None,
        performance_monitor: RuntimePerformanceMonitor | None = None,
        incident_sink: RuntimeIssueSink | None = None,
        event_queue_capacity: int = DEFAULT_EVENT_QUEUE_CAPACITY,
        event_queue_control_reserve: int = DEFAULT_CONTROL_RESERVE,
        event_queue_must_deliver_timeout: float = (
            DEFAULT_MUST_DELIVER_TIMEOUT_SECONDS
        ),
        event_queue_max_coalesced_chars: int = DEFAULT_MAX_COALESCED_CHARS,
        transcript_prewarm_batch_size: int = 32,
    ) -> None:
        self.root_agent_id = root_agent_id
        if session_generation is not None and (
            not isinstance(session_generation, int)
            or isinstance(session_generation, bool)
        ):
            raise TypeError("session generation must be an integer")
        self._generation_lock = threading.Lock()
        self._active_generation = session_generation
        self._stale_incident_dropped = 0
        self._performance_monitor = performance_monitor
        self._incident_sink = incident_sink
        self._queue_sample_lock = threading.Lock()
        self._last_queue_sample_at = 0.0
        self._incident_lock = threading.Lock()
        self._pending_incidents: dict[_UIIncident, int] = {}
        self._pending_incident_overflow = 0
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
        self._pending_events = BoundedUIEventQueue(
            capacity=event_queue_capacity,
            control_reserve=event_queue_control_reserve,
            must_deliver_timeout=event_queue_must_deliver_timeout,
            max_coalesced_chars=event_queue_max_coalesced_chars,
            generation_agent_id=root_agent_id,
        )
        if session_generation is not None:
            self._pending_events.advance_generation(session_generation)
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
        self._resize_target: (
            tuple[int, tuple[int, int, int, int]] | None
        ) = None
        self._transcript_prewarmer = TranscriptLayoutPrewarmer(
            on_complete=self._publish_transcript_prewarm,
            on_observation=self._record_transcript_cache_sample,
            on_failure=lambda error: self.report_projection_failure(
                "layout_prewarm", error
            ),
            batch_size=transcript_prewarm_batch_size,
        )

    def bind_invalidator(self, callback) -> None:
        self._invalidate = callback

    def bind_incident_sink(self, sink: RuntimeIssueSink | None) -> None:
        """Route content-free projection and delivery failures to the agent."""
        self._incident_sink = sink

    def report_projection_failure(
        self,
        subsystem: str,
        error: BaseException,
        *,
        request_repaint: bool = True,
    ) -> None:
        """Expose one secondary UI failure without leaking its message/content."""
        safe_subsystem = (
            subsystem
            if subsystem
            and len(subsystem) <= 64
            and subsystem.isascii()
            and subsystem.replace("_", "").isalnum()
            else "adapter"
        )
        self._retain_incident(
            _UIIncident(
                kind="projection",
                detail=safe_subsystem,
                error_type=_safe_error_type(error),
            )
        )
        if request_repaint:
            self._request_invalidate()

    def on_ui_event(self, event: UIEvent) -> EventPutResult:
        # Worker/model/tool threads only enqueue. Projection and rendering are
        # drained by prompt_toolkit's UI thread on the next paint.
        started_at = time.monotonic()
        result = self._pending_events.put(event)
        elapsed_ms = (time.monotonic() - started_at) * 1000
        failure_reason = result.reason
        visible_rejection = False
        if not result.accepted and failure_reason is not None:
            visible_rejection = failure_reason in {
                EventPutFailureReason.CLOSED,
                EventPutFailureReason.CONTROL_TIMEOUT,
            }
            if visible_rejection:
                self._retain_incident(
                    _UIIncident(kind="delivery", detail=failure_reason.value)
                )
        if not result.accepted or elapsed_ms >= 1:
            self._record_queue_sample(
                "enqueue",
                elapsed_ms,
                status="ok" if result.accepted else "dropped",
                failure_reason=failure_reason,
                force=True,
            )
        if result.wake_consumer or visible_rejection:
            self._request_invalidate()
        return result

    def _drain_pending_events(self) -> None:
        started_at = time.monotonic()
        pending = self._pending_events.drain()
        with self._lock:
            for event in pending:
                if self._pending_events.reject_stale(event):
                    continue
                try:
                    self._apply_pending_event_locked(event)
                except KeyboardInterrupt:
                    raise
                except BaseException as error:
                    self._retain_incident(
                        _UIIncident(
                            kind="projection",
                            detail="adapter",
                            error_type=_safe_error_type(error),
                        ),
                        session_generation=_event_generation(
                            event,
                            owner_agent_id=self.root_agent_id,
                        ),
                    )
            self._flush_incidents_locked()
        if pending:
            self._record_queue_sample(
                "drain",
                (time.monotonic() - started_at) * 1000,
                batch_size=len(pending),
            )

    def event_queue_stats(self) -> EventQueueStats:
        """Return a content-free snapshot of current TUI queue pressure."""
        return self._pending_events.stats()

    @property
    def stale_incident_dropped(self) -> int:
        with self._generation_lock:
            return self._stale_incident_dropped

    def close(self) -> None:
        """Stop accepting UI events and wake blocked producer threads."""
        self._transcript_prewarmer.cancel()
        self._pending_events.close()

    def transcript_cache_stats(self) -> TranscriptCacheStats:
        """Return content-free resize-cache counters for diagnostics."""
        return self._transcript_prewarmer.snapshot()

    def wait_for_transcript_prewarm(self, timeout: float = 2.0) -> bool:
        """Wait for tests/benchmarks; the live UI never blocks on this path."""
        return self._transcript_prewarmer.wait_idle(timeout)

    def _record_transcript_cache_sample(
        self, observation: TranscriptPrewarmObservation
    ) -> None:
        if observation.status == "error":
            with self._lock:
                if (
                    self._resize_target is not None
                    and self._resize_target[0] == observation.generation
                ):
                    self._resize_target = None
        monitor = self._performance_monitor
        if monitor is None:
            return
        try:
            attributes: dict[str, int | str] = {
                "generation": observation.generation,
                "width": observation.width,
                "cell_count": observation.cell_count,
                "batches": observation.batches,
                "cache_hits": observation.cache_hits,
                "cache_misses": observation.cache_misses,
                "render_rows": observation.render_rows,
            }
            if observation.error_type is not None:
                attributes["error_type"] = observation.error_type
            monitor.record(
                "tui_cache",
                "resize_prewarm",
                observation.elapsed_ms,
                status=observation.status,
                attributes=attributes,
            )
        except BaseException as error:
            self._retain_incident(
                _UIIncident(
                    kind="monitor",
                    detail="transcript_cache",
                    error_type=_safe_error_type(error),
                )
            )
            self._request_invalidate()

    def _cancel_transcript_prewarm_locked(self) -> None:
        if self._resize_target is None:
            return
        self._resize_target = None
        self._transcript_prewarmer.cancel()

    def _schedule_transcript_prewarm_locked(
        self,
        source_key: tuple[int, int, int, int],
    ) -> bool:
        if (
            self._transcript_layout_source_key is None
            or not self._transcript_layout.cells
        ):
            return False
        if self._resize_target is not None and self._resize_target[1] == source_key:
            return True
        model = self.transcript.state.transcript
        placements = tuple(compose_transcript(model.cells))
        render_key = tuple(
            visual_cell_key(
                placement,
                width=source_key[2],
                theme_revision=source_key[3],
            )
            for placement in placements
        )
        cached_lines = {
            key: lines
            for key in render_key
            if (lines := self._cell_visual_cache.get(key)) is not None
        }
        generation = self._transcript_prewarmer.request(
            source_key=source_key,
            render_key=render_key,
            placements=placements,
            cached_lines=cached_lines,
        )
        self._resize_target = (generation, source_key)
        if not self._transcript_prewarmer.snapshot().in_flight:
            self._resize_target = None
            return False
        return True

    def _publish_transcript_prewarm(
        self, result: TranscriptPrewarmResult
    ) -> bool:
        with self._lock:
            model = self.transcript.state.transcript
            current_source_key = (
                id(model),
                model.revision,
                self._viewport_width,
                self._theme_revision,
            )
            if self._resize_target != (result.generation, result.source_key):
                return False
            if result.source_key != current_source_key:
                return False
            placements = compose_transcript(model.cells)
            current_render_key = tuple(
                visual_cell_key(
                    placement,
                    width=self._viewport_width,
                    theme_revision=self._theme_revision,
                )
                for placement in placements
            )
            if current_render_key != result.render_key:
                return False
            self._cell_visual_cache.update(result.cache_entries)
            live_keys = set(result.render_key)
            if len(self._cell_visual_cache) > max(50, len(live_keys) * 2):
                self._cell_visual_cache = {
                    key: value
                    for key, value in self._cell_visual_cache.items()
                    if key in live_keys
                }
            self._placement_by_id = {
                placement.cell.id: placement for placement in placements
            }
            self._transcript_layout_key = result.render_key
            self._transcript_layout_source_key = result.source_key
            self._transcript_layout = VirtualTranscriptLayout(result.visual_cells)
            self._layout_model_revision = model.revision
            self._layout_structure_dirty = False
            self._layout_dirty_ids.clear()
            self._flattened_layout = None
            self._resize_target = None
        self._request_invalidate()
        return True

    def _record_queue_sample(
        self,
        name: str,
        elapsed_ms: float,
        *,
        status: str = "ok",
        batch_size: int | None = None,
        failure_reason: EventPutFailureReason | None = None,
        force: bool = False,
    ) -> None:
        monitor = self._performance_monitor
        if monitor is None:
            return
        try:
            observed_at = time.monotonic()
            with self._queue_sample_lock:
                if (
                    not force
                    and observed_at - self._last_queue_sample_at
                    < _QUEUE_SAMPLE_INTERVAL_SECONDS
                ):
                    return
                self._last_queue_sample_at = observed_at
            stats = self._pending_events.stats()
            attributes: dict[str, int | str] = {
                "capacity": stats.capacity,
                "depth": stats.depth,
                "high_watermark": stats.high_watermark,
                "coalesced": stats.coalesced,
                "transient_dropped": stats.transient_dropped,
                "must_deliver_waits": stats.must_deliver_waits,
                "must_deliver_timeouts": stats.must_deliver_timeouts,
                "closed_dropped": stats.closed_dropped,
                "stale_generation_dropped": stats.stale_generation_dropped,
                "stale_incident_dropped": self.stale_incident_dropped,
            }
            if batch_size is not None:
                attributes["batch_size"] = batch_size
            if failure_reason is not None:
                attributes["failure_reason"] = failure_reason.value
            monitor.record(
                "ui_queue",
                name,
                elapsed_ms,
                status=status,
                attributes=attributes,
            )
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._retain_incident(
                _UIIncident(
                    kind="monitor",
                    detail="performance",
                    error_type=_safe_error_type(error),
                )
            )
            # Drain sampling happens after the current incident flush, so
            # arrange one more paint to make this secondary failure visible.
            self._request_invalidate()

    def _apply_pending_event_locked(self, event: UIEvent) -> None:
        if isinstance(event.payload, RuntimeEventPayload):
            runtime = event.payload.event
            generation = _event_generation(
                event,
                owner_agent_id=self.root_agent_id,
            )
            handler = self.runtime_event_handler
            if handler is not None:
                self._apply_projection_step(
                    "runtime_handler",
                    lambda: handler(runtime),
                    session_generation=generation,
                )
            self._apply_projection_step(
                "execution",
                lambda: self.execution.apply(runtime),
                session_generation=generation,
            )
            if self._is_root_transcript_event(runtime):
                self._apply_projection_step(
                    "transcript",
                    lambda: self._record_presentation_changes(
                        self.transcript.apply(runtime)
                    ),
                    session_generation=generation,
                )
            return
        if isinstance(event.payload, InteractionPromptPayload):
            request = event.payload.request
            if isinstance(request, ReviewRequest):
                self._apply_projection_step(
                    "transcript",
                    lambda: self._record_presentation_changes(
                        self.transcript.hydrate_approval(
                            request_id=request.request_id,
                            title=request.title,
                            summary=request.summary,
                            sections=request.sections,
                        )
                    ),
                )
            return
        self._apply_projection_step(
            "transcript", lambda: self._project_passive_event_locked(event)
        )

    def _project_passive_event_locked(self, event: UIEvent) -> None:
        message = event.message
        if isinstance(event.payload, ViewEventPayload):
            payload = event.payload
            # Interactive presenters (selection panels) own their view types,
            # for both opens and refreshes; they suppress passive projection.
            handler = self.interactive_view_handler
            if handler is not None:
                succeeded, claimed = self._apply_projection_step(
                    "interactive", lambda: handler(payload)
                )
                if succeeded and claimed:
                    return
            if payload.view_type == "session_resume" and isinstance(
                payload.view_model, SessionResumeViewModel
            ):
                model = payload.view_model
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
                message = _view_text(payload)
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

    def _apply_projection_step(
        self,
        subsystem: str,
        callback: Callable[[], object],
        *,
        session_generation: int | None = None,
    ) -> tuple[bool, object | None]:
        try:
            return True, callback()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._retain_incident(
                _UIIncident(
                    kind="projection",
                    detail=subsystem,
                    error_type=_safe_error_type(error),
                ),
                session_generation=session_generation,
            )
            return False, None

    def _request_invalidate(self) -> None:
        try:
            self._invalidate()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._retain_incident(
                _UIIncident(
                    kind="invalidate",
                    detail="paint",
                    error_type=_safe_error_type(error),
                )
            )

    def _retain_incident(
        self,
        incident: _UIIncident,
        *,
        session_generation: int | None = None,
    ) -> None:
        """Report a safe fact to the agent and retain a local UI warning."""
        with self._generation_lock:
            active_generation = self._active_generation
        generation = (
            session_generation
            if isinstance(session_generation, int)
            and not isinstance(session_generation, bool)
            else active_generation
        )
        observer_incident: _UIIncident | None = None
        sink = self._incident_sink
        if sink is not None:
            phase, error_type, ref = _incident_runtime_facts(incident)
            try:
                accepted = deliver_runtime_issue(
                    sink,
                    phase,
                    error_type,
                    ref,
                    1,
                    agent_id=self.root_agent_id,
                    session_generation=generation,
                )
            except KeyboardInterrupt:
                raise
            except BaseException as error:
                observer_incident = _UIIncident(
                    kind="observer",
                    detail="incident_sink",
                    error_type=_safe_error_type(error),
                )
            else:
                if accepted is False:
                    self._record_stale_incident_drop()
                    return
        self._retain_local_incident(incident, generation=generation)
        if observer_incident is not None:
            # Do not route a failure of the incident sink back into that sink.
            self._retain_local_incident(observer_incident, generation=generation)

    def _record_stale_incident_drop(self) -> None:
        with self._generation_lock:
            self._stale_incident_dropped = min(
                self._stale_incident_dropped + 1,
                _MAX_INCIDENT_COUNT,
            )

    def _retain_local_incident(
        self,
        incident: _UIIncident,
        *,
        generation: int | None,
    ) -> None:
        """Keep one bounded local diagnostic; carrier failure stays isolated."""
        try:
            with self._generation_lock:
                if (
                    generation is not None
                    and self._active_generation is not None
                    and generation != self._active_generation
                ):
                    self._stale_incident_dropped = min(
                        self._stale_incident_dropped + 1,
                        _MAX_INCIDENT_COUNT,
                    )
                    return
                with self._incident_lock:
                    current = self._pending_incidents.get(incident)
                    if current is not None:
                        self._pending_incidents[incident] = min(
                            current + 1,
                            _MAX_INCIDENT_COUNT,
                        )
                    elif len(self._pending_incidents) < _MAX_PENDING_INCIDENT_KEYS:
                        self._pending_incidents[incident] = 1
                    else:
                        self._pending_incident_overflow = min(
                            self._pending_incident_overflow + 1,
                            _MAX_INCIDENT_COUNT,
                        )
        except KeyboardInterrupt:
            raise
        except BaseException:
            pass

    def _flush_incidents_locked(self) -> None:
        try:
            with self._incident_lock:
                incidents = tuple(self._pending_incidents.items())
                overflow = self._pending_incident_overflow
                self._pending_incidents.clear()
                self._pending_incident_overflow = 0
        except KeyboardInterrupt:
            raise
        except BaseException:
            return
        if overflow:
            incidents += ((_UIIncident(kind="overflow", detail="overflow"), overflow),)
        for incident, count in incidents:
            try:
                self._notice_seq += 1
                self._record_presentation_changes(
                    self.transcript.append_notice(
                        notice_id=f"ui-incident:{self._notice_seq}",
                        message=_incident_message(incident, count),
                        level="warning",
                        category="ui",
                    )
                )
            except KeyboardInterrupt:
                raise
            except BaseException:
                # An incident must never recursively report its own projection.
                continue

    def _record_presentation_changes(self, changes) -> None:
        changed = False
        for change in changes:
            changed = True
            if change.kind is PresentationChangeKind.UPDATE and change.cell is not None:
                self._layout_dirty_ids.add(change.cell.id)
            else:
                self._layout_structure_dirty = True
        if changed:
            self._cancel_transcript_prewarm_locked()

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
            self._cancel_transcript_prewarm_locked()
        self._request_invalidate()

    def _advance_generation(self, generation: int) -> None:
        stale_events = self._pending_events.advance_generation(generation)
        with self._generation_lock:
            dropped = 0
            if generation != self._active_generation:
                previous_generation = self._active_generation
                self._active_generation = generation
                if previous_generation is not None:
                    with self._incident_lock:
                        dropped = sum(self._pending_incidents.values())
                        dropped += self._pending_incident_overflow
                        self._pending_incidents.clear()
                        self._pending_incident_overflow = 0
                    self._stale_incident_dropped = min(
                        self._stale_incident_dropped + dropped,
                        _MAX_INCIDENT_COUNT,
                    )
        if stale_events or dropped:
            self._record_queue_sample(
                "generation_advance",
                0.0,
                status="dropped",
                batch_size=min(stale_events + dropped, _MAX_INCIDENT_COUNT),
                force=True,
            )

    def restore_control_state(self, plan, progress, *, session_id: str | None) -> None:
        """Replace projections after an explicit session/new-context switch."""
        self._advance_generation(plan.session_generation)
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
        self._request_invalidate()

    def clear_transcript(self) -> None:
        """Clear only the visible canvas while preserving persisted session history."""
        with self._lock:
            self._cancel_transcript_prewarm_locked()
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
            self._resize_target = None
        self._request_invalidate()

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
            self._cancel_transcript_prewarm_locked()
        self._request_invalidate()

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
            if (
                self._resize_target is not None
                and self._resize_target[1] == source_key
            ):
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
        placements = compose_transcript(cells)
        render_key = tuple(
            visual_cell_key(
                placement,
                width=self._viewport_width,
                theme_revision=self._theme_revision,
            )
            for placement in placements
        )
        if render_key == self._transcript_layout_key:
            return self._transcript_layout
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
        """Keep the old layout usable while a changed width prewarms."""
        previous = self._transcript_layout
        anchor = previous.anchor_at(scroll_line)
        self._drain_pending_events()
        self.set_viewport_width(width)
        with self._lock:
            model = self.transcript.state.transcript
            source_key = (
                id(model),
                model.revision,
                self._viewport_width,
                self._theme_revision,
            )
            width_changed = (
                self._transcript_layout_source_key is not None
                and self._transcript_layout_source_key[2] != self._viewport_width
            )
            if width_changed and self._schedule_transcript_prewarm_locked(source_key):
                return previous, scroll_line
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
