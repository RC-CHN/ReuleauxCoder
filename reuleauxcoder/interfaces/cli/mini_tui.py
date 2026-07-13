"""prompt_toolkit-owned interactive CLI viewport.

The adapter intentionally contains no Plan or agent business state.  It renders
the shared presentation reducers and turns bottom-pane input into shared
interaction responses.
"""

from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import time
from typing import Any

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
from reuleauxcoder.app.runtime.session_state import build_session_persistence_kwargs
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
    SubagentJobChanged,
)
from reuleauxcoder.domain.approval import ApprovalSectionKind
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.cli.virtual_transcript import (
    VirtualTranscriptControl,
    VirtualTranscriptLayout,
    VisualCell,
)
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeEventPayload,
    UIEvent,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ChooseOneResponse,
    ConfirmRequest,
    ConfirmResponse,
    InputTextRequest,
    InputTextResponse,
    ReviewRequest,
    ReviewResponse,
)
from reuleauxcoder.presentation import (
    ApprovalCell,
    AssistantCell,
    DiagnosticCell,
    DiffCell,
    ExecutionViewReducer,
    NoticeCell,
    PresentationReducer,
    RuntimeViewState,
    SubagentCell,
    ToolCell,
    TranscriptModel,
    UserCell,
    execution_panel_lines,
)


MINI_TUI_STYLE = Style.from_dict(
    {
        "frame.label": "bold #071013 bg:#67e8f9",
        "frame.border": "#64748b",
        "panel.header": "bold #f8fafc bg:#334155",
        "panel.body": "#dbeafe",
        "user": "bold #ffffff bg:#5b4bc4",
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


class MiniTUIEventAdapter:
    """Thread-safe, source-backed event projection for the mini-TUI."""

    def __init__(self, *, root_agent_id: str | None = None) -> None:
        self.root_agent_id = root_agent_id
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
        self._transcript_layout = VirtualTranscriptLayout(())
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
            while True:
                try:
                    event = self._pending_events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._apply_pending_event_locked(event)
                except Exception as error:
                    # A malformed view projection must not stop the agent or the
                    # viewport. Keep one bounded diagnostic in the transcript.
                    self._notice_seq += 1
                    self.transcript.append_notice(
                        notice_id=f"ui-projection:{self._notice_seq}",
                        message=f"UI projection skipped: {error}",
                        level="warning",
                        category="ui",
                    )

    def _apply_pending_event_locked(self, event: UIEvent) -> None:
        if isinstance(event.payload, RuntimeEventPayload):
            runtime = event.payload.event
            self.execution.apply(runtime)
            if self._is_root_transcript_event(runtime):
                self.transcript.apply(runtime)
            return
        if isinstance(event.payload, InteractionPromptPayload):
            request = event.payload.request
            if isinstance(request, ReviewRequest):
                self.transcript.hydrate_approval(
                    request_id=request.request_id,
                    title=request.title,
                    summary=request.summary,
                    sections=request.sections,
                )
            return
        message = event.message
        if isinstance(event.payload, ViewEventPayload):
            if event.payload.view_type == "session_resume" and hasattr(
                event.payload.view_model, "entries"
            ):
                model = event.payload.view_model
                for index, entry in enumerate(model.entries):
                    cell_id = f"restored:{index}:{self._notice_seq}"
                    if entry.role == "user":
                        self.transcript.state.transcript.append(
                            UserCell(id=cell_id, text=entry.content)
                        )
                    elif entry.role == "assistant":
                        self.transcript.state.transcript.append(
                            AssistantCell(
                                id=cell_id,
                                text=entry.content,
                                complete=True,
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
            self.transcript.append_notice(
                notice_id=f"ui:{event.timestamp}:{self._notice_seq}",
                message=message,
                level=event.level.value,
                category=event.kind.value,
            )

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
            self.transcript.append_notice(
                notice_id=f"command:{self._notice_seq}",
                message=text,
                level="user",
                category="user",
            )
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
            self._transcript_layout = VirtualTranscriptLayout(())
            self._flattened_layout = None
            self._transcript_rendered = FormattedText()
        self._invalidate()

    def append_restored_conversation(self, entries) -> None:
        """Replay a bounded human transcript without adding model history."""
        with self._lock:
            for index, entry in enumerate(entries):
                role = entry.get("role")
                content = str(entry.get("content") or "")
                cell_id = f"restored:{index}:{self._notice_seq}"
                if role == "user":
                    self.transcript.state.transcript.append(
                        UserCell(id=cell_id, text=content)
                    )
                elif role == "assistant":
                    self.transcript.state.transcript.append(
                        AssistantCell(id=cell_id, text=content, complete=True)
                    )
            self._notice_seq += 1
        self._invalidate()

    def panel_lines(self, width: int) -> tuple[str, ...]:
        self._drain_pending_events()
        with self._lock:
            return execution_panel_lines(self.execution.state, width=width)

    def set_viewport_width(self, width: int) -> None:
        self._viewport_width = max(20, width)

    def has_animation_lease(self) -> bool:
        now = time.time()
        with self._lock:
            return any(
                agent.is_animating(now)
                for agent in self.execution.state.agents.values()
            )

    def transcript_layout(self, width: int | None = None) -> VirtualTranscriptLayout:
        self._drain_pending_events()
        if width is not None:
            self.set_viewport_width(width)
        with self._lock:
            cells = self.transcript.state.transcript.cells
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
        live_keys: set[tuple[str, int, int, int]] = set()
        visual_cells: list[VisualCell] = []
        for cell in cells:
            key = (
                cell.id,
                cell.revision,
                self._viewport_width,
                self._theme_revision,
            )
            live_keys.add(key)
            lines = self._cell_visual_cache.get(key)
            if lines is None:
                lines = _fragments_to_visual_lines(
                    _wrap_fragments(
                        _cell_fragments(
                            cell,
                            width=self._viewport_width,
                            markdown_renderer=self._markdown,
                        ),
                        width=max(1, self._viewport_width),
                    )
                )
                self._cell_visual_cache[key] = lines
            visual_cells.append(VisualCell(key=key, lines=lines))
        if len(self._cell_visual_cache) > max(50, len(live_keys) * 2):
            self._cell_visual_cache = {
                key: value
                for key, value in self._cell_visual_cache.items()
                if key in live_keys
            }
        self._transcript_layout_key = render_key
        self._transcript_layout = VirtualTranscriptLayout(tuple(visual_cells))
        self._flattened_layout = None
        return self._transcript_layout

    def transcript_fragments(self) -> FormattedText:
        """Compatibility projection; the live Window resolves visual rows lazily."""
        layout = self.transcript_layout()
        if self._flattened_layout is layout:
            return self._transcript_rendered
        self._transcript_rendered = FormattedText(layout.flatten())
        self._flattened_layout = layout
        return self._transcript_rendered


class MiniTUIInteractor:
    """Blocking UIInteractor whose requests are answered by the bottom pane."""

    def __init__(self, ui_bus) -> None:
        self.ui_bus = ui_bus
        self._condition = threading.Condition()
        self._active: Any | None = None
        self._response: Any | None = None
        self._invalidate = lambda: None

    @property
    def active_request(self):
        with self._condition:
            return self._active

    def bind_invalidator(self, callback) -> None:
        self._invalidate = callback

    def notify(self, event: UIEvent) -> None:
        self.ui_bus.emit(event)

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        return self._ask(request)

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        if not request.items:
            return ChooseOneResponse(None, cancelled=True)
        return self._ask(request)

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        return self._ask(request)

    def review(self, request: ReviewRequest) -> ReviewResponse:
        return self._ask(request)

    def submit(self, text: str) -> bool:
        with self._condition:
            request = self._active
            if request is None:
                return False
            response = _interaction_response(request, text)
            if response is None:
                return True
            self._response = response
            self._active = None
            self._condition.notify_all()
        self._invalidate()
        return True

    def cancel(self, request_id: str) -> None:
        with self._condition:
            request = self._active
            if request is None or request.request_id != request_id:
                return
            self._response = _cancelled_response(request, "interaction cancelled")
            self._active = None
            self._condition.notify_all()
        self._invalidate()

    def cancel_active(self, reason: str = "interaction interrupted") -> bool:
        with self._condition:
            request = self._active
            if request is None:
                return False
            self._response = _cancelled_response(request, reason)
            self._active = None
            self._condition.notify_all()
        self._invalidate()
        return True

    def _ask(self, request):
        with self._condition:
            if self._active is not None:
                raise RuntimeError("mini-TUI interaction slot is already occupied")
            self._active = request
            self._response = None
        self.ui_bus.emit_interaction_prompt(request)
        self._invalidate()
        with self._condition:
            while self._active is request:
                timeout = 0.1
                if request.deadline is not None:
                    remaining = request.deadline - time.monotonic()
                    if remaining <= 0:
                        self._active = None
                        self._response = _cancelled_response(
                            request, "interaction deadline exceeded"
                        )
                        break
                    timeout = min(timeout, remaining)
                self._condition.wait(timeout)
            response = self._response
            self._response = None
            return response


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
        interactor: MiniTUIInteractor,
        event_adapter: MiniTUIEventAdapter,
        current_session_id: str | None,
        sessions_dir: Path | None,
        session_exit_time: str | None,
        skills_service=None,
        startup_events: tuple[UIEvent, ...] = (),
    ) -> None:
        self.agent = agent
        self.config = config
        self.ui_bus = ui_bus
        self.ui_profile = ui_profile
        self.action_registry = action_registry
        self.interactor = interactor
        self.events = event_adapter
        self.current_session_id = current_session_id
        self.sessions_dir = sessions_dir
        self.session_exit_time = session_exit_time
        self.skills_service = skills_service
        self.running = False
        self.cancelling = False
        self.exit_confirm = False
        self._closed = False
        self._worker: threading.Thread | None = None
        self._animation_stop = threading.Event()
        self._animation_thread: threading.Thread | None = None
        self._width = 100
        self._follow_transcript = True
        self._transcript_max_scroll = 0
        self._last_terminal_rows = 0
        self.session_header_expanded = True
        self.startup_lines = tuple(
            event.message.splitlines()[0]
            for event in startup_events
            if event.message.strip()
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
        self.transcript_control = VirtualTranscriptControl(
            self.events.transcript_layout,
            self._transcript_cursor_position,
        )
        self.transcript_window = Window(
            self.transcript_control,
            wrap_lines=False,
            always_hide_cursor=True,
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
        self.input_window = Window(
            BufferControl(buffer=self.input_buffer),
            height=1,
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
        text = buffer.text.strip()
        buffer.reset()
        if self.interactor.active_request is not None:
            self.interactor.submit(text)
            return True
        if not text:
            return True
        if self.running:
            if self.agent.submit_user_steering(text):
                self.events.append_user_command(text)
                self.ui_bus.info("Direction queued for the next safe boundary.")
            return True
        self.exit_confirm = False
        self.session_header_expanded = False
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

    def _handle_input(self, user_input: str) -> None:
        try:
            previous_session_id = self.current_session_id
            previous_generation = self.agent.session_generation
            if user_input.startswith("/"):
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
                self.transcript_pane.vertical_scroll = 0
            if result["action"] == "continue" and session_changed:
                self.events.restore_control_state(
                    self.agent.plan_controller.state,
                    self.agent.plan_controller.progress,
                    session_id=self.current_session_id,
                )
            if result["action"] == "exit":
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
            self.running = False
            self.cancelling = False
            self.invalidate()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        transcript_arrow_scroll = Condition(
            self._should_route_arrows_to_transcript
        )

        @bindings.add("c-c")
        def _ctrl_c(event) -> None:
            if self.interactor.cancel_active():
                self.input_buffer.reset()
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
                    self.ui_bus.warning(
                        "Cancelling at the next protocol-safe boundary…"
                    )
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
            self.transcript_pane.vertical_scroll = 0
            self.invalidate()

        @bindings.add("end")
        def _history_end(event) -> None:  # noqa: ARG001
            self._follow_transcript = True
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
        lines = self._panel_lines()
        return FormattedText(
            [
                (
                    "class:panel.header" if index == 0 else "class:panel.body",
                    line + "\n",
                )
                for index, line in enumerate(lines)
            ]
        )

    def _panel_height(self) -> int:
        return max(3, min(12, len(self._panel_lines())))

    def _panel_lines(self) -> tuple[str, ...]:
        lines = list(self.events.panel_lines(self._width))
        if self.session_header_expanded:
            details = [
                f"MODEL {self.config.model}",
                f"ROOT  {Path.cwd()}",
                f"SESSION {self.current_session_id or 'new'}",
                *self.startup_lines,
            ]
            lines[1:1] = details[:8]
        return tuple(_clip(line, self._width) for line in lines)

    def _interaction_height(self) -> int:
        request = self.interactor.active_request
        if request is None:
            return 1
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
            return FormattedText([("class:warning", "Cancelling safely…\n")])
        if self.running:
            return FormattedText(
                [("class:muted", "Agent running · Ctrl+C interrupts\n")]
            )
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
        return (
            self.interactor.active_request is None
            and not self.input_buffer.text
        )

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
            content_height = self.events.transcript_layout(
                content_width
            ).line_count
            resized = size.rows != self._last_terminal_rows
            viewport = max(
                1,
                (
                    size.rows
                    - self._panel_height()
                    - self._interaction_height()
                    - 6
                    if resized or not self.transcript_control.last_height
                    else self.transcript_control.last_height
                ),
            )
            self._last_terminal_rows = size.rows
            maximum = max(0, content_height - viewport)
            self._transcript_max_scroll = maximum
            if self._follow_transcript:
                self.transcript_pane.vertical_scroll = maximum
            else:
                self.transcript_pane.vertical_scroll = min(
                    self.transcript_pane.vertical_scroll, maximum
                )
                if self.transcript_pane.vertical_scroll >= maximum:
                    self._follow_transcript = True
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
        pane = getattr(self, "transcript_pane", None)
        return Point(x=0, y=max(0, getattr(pane, "vertical_scroll", 0)))

    def _scroll_transcript(self, delta: int) -> None:
        target = max(
            0,
            min(
                self._transcript_max_scroll,
                self.transcript_pane.vertical_scroll + delta,
            ),
        )
        self.transcript_pane.vertical_scroll = target
        # Scrolling up opts out of tail-follow. Returning to the current bottom
        # opts back in, so subsequent streaming chunks remain visible.
        self._follow_transcript = target >= self._transcript_max_scroll
        self.invalidate()

    def _save_exit_session(self) -> None:
        self._closed = True
        self._prepare_forced_exit("CLI session closed")
        if not self.agent.messages or not self.config.session_auto_save:
            return
        sid = SessionStore(self.sessions_dir).save(
            self.agent.messages,
            self.config.model,
            self.current_session_id,
            is_exit=True,
            total_prompt_tokens=self.agent.state.total_prompt_tokens,
            total_completion_tokens=self.agent.state.total_completion_tokens,
            active_mode=getattr(self.agent, "active_mode", None),
            **build_session_persistence_kwargs(self.agent),
        )
        self.agent.lifecycle.session_saved(sid)

    def _prepare_forced_exit(self, reason: str) -> None:
        self.agent.request_stop()
        self.interactor.cancel_active(reason)
        reconcile = getattr(self.agent, "reconcile_pending_tool_calls", None)
        if callable(reconcile):
            reconcile(reason)


def _cell_fragments(
    cell,
    *,
    width: int = 100,
    markdown_renderer: RetainedMarkdownRenderer | None = None,
) -> list[tuple[str, str]]:
    if isinstance(cell, UserCell):
        return [("class:user", f" YOU  {cell.text}\n"), ("", "\n")]
    if isinstance(cell, AssistantCell):
        renderer = markdown_renderer or RetainedMarkdownRenderer()
        return renderer.render(
            cell_id=cell.id,
            revision=cell.revision,
            text=cell.text,
            complete=cell.complete,
            width=width,
        )
    if isinstance(cell, ToolCell):
        status = cell.status.value.upper()
        style = "class:error" if cell.status.value == "failed" else "class:tool"
        text = f" {status}  {cell.name}"
        if cell.outcome is not None:
            summary = cell.outcome.summary or _first_meaningful_line(
                cell.outcome.ui_text(include_details=True)
            )
            if summary:
                text += f" · {_clip(summary, 160)}"
        fragments = [(style, text + "\n")]
        for line in cell.output.splitlines()[-5:]:
            fragments.append(("class:muted", f" └ {line}\n"))
        if cell.status.value != "running":
            fragments.append(("", "\n"))
        return fragments
    if isinstance(cell, DiffCell):
        fragments: list[tuple[str, str]] = []
        for line in cell.diff.splitlines():
            style = "class:diff.header"
            if line.startswith("+") and not line.startswith("+++"):
                style = "class:diff.add"
            elif line.startswith("-") and not line.startswith("---"):
                style = "class:diff.del"
            fragments.append((style, line + "\n"))
        fragments.append(("", "\n"))
        return fragments
    if isinstance(cell, NoticeCell):
        if cell.category == "user" or cell.level == "user":
            return [("class:user", f" YOU  {cell.message}\n")]
        style = {
            "error": "class:error",
            "warning": "class:warning",
            "success": "class:success",
        }.get(cell.level, "class:muted")
        return [(style, cell.message + "\n")]
    if isinstance(cell, ApprovalCell):
        return _approval_fragments(cell, width=width)
    if isinstance(cell, SubagentCell):
        return [
            ("class:tool", f" AGENT  {cell.job_id} · {cell.status} · {cell.task}\n")
        ]
    if isinstance(cell, DiagnosticCell):
        return [
            (
                "class:warning",
                f" LSP  {cell.path} · {len(cell.diagnostics)} diagnostic(s)\n",
            )
        ]
    return [("class:muted", str(cell) + "\n")]


def _wrap_fragments(
    fragments: list[tuple[str, str]], *, width: int
) -> list[tuple[str, str]]:
    """Pre-wrap styled fragments so Window scroll units equal visual rows."""
    width = max(1, width)
    output: list[tuple[str, str]] = []
    column = 0
    for style, text in fragments:
        chunk = ""
        for character in text:
            if character == "\n":
                if chunk:
                    output.append((style, chunk))
                    chunk = ""
                output.append(("", "\n"))
                column = 0
                continue
            character_width = max(0, get_cwidth(character))
            if column and column + character_width > width:
                if chunk:
                    output.append((style, chunk))
                    chunk = ""
                output.append(("", "\n"))
                column = 0
            chunk += character
            column += character_width
        if chunk:
            output.append((style, chunk))
    return output


def _fragments_to_visual_lines(
    fragments: list[tuple[str, str]],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    lines: list[list[tuple[str, str]]] = [[]]
    for style, text in fragments:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if part:
                if lines[-1] and lines[-1][-1][0] == style:
                    previous_style, previous_text = lines[-1][-1]
                    lines[-1][-1] = (previous_style, previous_text + part)
                else:
                    lines[-1].append((style, part))
            if index + 1 < len(parts):
                lines.append([])
    return tuple(tuple(line) for line in lines)


def _approval_fragments(cell: ApprovalCell, *, width: int) -> list[tuple[str, str]]:
    """Render the v0.4-style review card in the scrollable transcript."""
    frame_width = max(24, min(100, width - 1))
    inner = frame_width - 4
    status = cell.status.upper()
    title = f" {cell.title.upper()} · {status} "
    title = _fit_display(title, frame_width - 4)
    rule = "━" * max(1, frame_width - get_cwidth(title) - 3)
    state_style = {
        "approved": "class:review.approved",
        "denied": "class:review.denied",
    }.get(cell.status, "class:review.border")
    title_style = {
        "approved": "class:review.title.approved",
        "denied": "class:review.title.denied",
    }.get(cell.status, "class:review.title.pending")
    fragments: list[tuple[str, str]] = [
        (state_style, "┏━"),
        (title_style, title),
        (state_style, f"{rule}┓\n"),
    ]

    def add_line(style: str, text: str = "") -> None:
        fitted = _fit_display(text, inner)
        padding = " " * max(0, inner - get_cwidth(fitted))
        fragments.append((state_style, "┃ "))
        fragments.append((style, fitted))
        fragments.append(("class:review.body", padding))
        fragments.append((state_style, " ┃\n"))

    if cell.summary:
        for line in cell.summary.splitlines():
            add_line("class:review.body", line)
    for section in cell.sections:
        add_line("class:panel.header", f" {section.title.upper()} ")
        content = section.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        lines = content.splitlines()
        visible = lines[:20]
        for line in visible:
            style = "class:review.body"
            if section.kind is ApprovalSectionKind.DIFF:
                if line.startswith("+") and not line.startswith("+++"):
                    style = "class:diff.add"
                elif line.startswith("-") and not line.startswith("---"):
                    style = "class:diff.del"
                elif line.startswith(("@@", "+++", "---")):
                    style = "class:diff.header"
            add_line(style, line)
        if len(lines) > len(visible):
            add_line("class:muted", f"… {len(lines) - len(visible)} more lines")
    if not cell.sections and cell.preview:
        add_line("class:muted", cell.preview)
    if cell.reason:
        reason_style = "class:success" if cell.status == "approved" else "class:error"
        add_line(reason_style, cell.reason)
    fragments.extend(
        [
            (state_style, f"┗{'━' * (frame_width - 2)}┛\n"),
            ("", "\n"),
        ]
    )
    return fragments


def _fit_display(text: str, width: int) -> str:
    """Clip a review row by terminal cell width, including CJK and emoji."""
    if get_cwidth(text) <= width:
        return text
    target = max(1, width - 1)
    result: list[str] = []
    used = 0
    for char in text:
        char_width = max(0, get_cwidth(char))
        if used + char_width > target:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def _first_meaningful_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _interaction_lines(request) -> list[str]:
    lines = [request.title]
    if isinstance(request, ReviewRequest):
        lines = [f"[Enter/Y] {request.approve_label}   [N] {request.reject_label}"]
    elif isinstance(request, ConfirmRequest):
        lines.extend([request.message, "[Enter/Y] Confirm   [N] Cancel"])
    elif isinstance(request, ChooseOneRequest):
        if request.message:
            lines.append(request.message)
        lines.extend(
            f"[{index}] {item.label}" for index, item in enumerate(request.items, 1)
        )
    elif isinstance(request, InputTextRequest):
        lines.append(request.prompt)
    return lines


def _interaction_response(request, text: str):
    answer = text.strip().lower()
    if isinstance(request, ReviewRequest):
        if answer in {"", "1", "y", "yes"}:
            return ReviewResponse(True)
        if answer in {"2", "n", "no"}:
            return ReviewResponse(False)
        return None
    if isinstance(request, ConfirmRequest):
        if answer in {"", "y", "yes"}:
            return ConfirmResponse(True)
        if answer in {"n", "no"}:
            return ConfirmResponse(False)
        return None
    if isinstance(request, ChooseOneRequest):
        if not answer and request.allow_cancel:
            return ChooseOneResponse(None, cancelled=True)
        if answer.isdigit() and 1 <= int(answer) <= len(request.items):
            return ChooseOneResponse(request.items[int(answer) - 1].id)
        return None
    if isinstance(request, InputTextRequest):
        value = text if text else request.initial_value
        if value or request.allow_empty:
            return InputTextResponse(value)
        return InputTextResponse(None, cancelled=True)
    return None


def _cancelled_response(request, reason: str):
    if isinstance(request, ReviewRequest):
        return ReviewResponse(False, cancelled=True, reason=reason)
    if isinstance(request, ConfirmRequest):
        return ConfirmResponse(False, cancelled=True)
    if isinstance(request, ChooseOneRequest):
        return ChooseOneResponse(None, cancelled=True)
    return InputTextResponse(None, cancelled=True)


def _view_text(payload: ViewEventPayload) -> str:
    model = payload.view_model
    if payload.view_type == "session_resume" and hasattr(model, "entries"):
        lines = [f"RESTORED {model.session_id} · {model.model} · {model.saved_at[:19]}"]
        lines.extend(
            f"{'YOU' if entry.role == 'user' else 'AGENT'}  {entry.content}"
            for entry in model.entries
        )
        return "\n".join(lines)
    if hasattr(model, "to_payload"):
        try:
            return json.dumps(model.to_payload(), ensure_ascii=False, indent=2)
        except Exception:
            pass
    if hasattr(model, "to_dict"):
        try:
            return json.dumps(model.to_dict(), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return f"{payload.title}: {model}"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"
