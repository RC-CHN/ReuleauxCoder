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
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from reuleauxcoder import __version__
from reuleauxcoder.app.runtime.session_state import build_session_persistence_kwargs
from reuleauxcoder.domain.runtime.events import (
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.commands import handle_command
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
    SubagentCell,
    ToolCell,
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
    }
)


class MiniTUIEventAdapter:
    """Thread-safe, source-backed event projection for the mini-TUI."""

    def __init__(self, *, root_agent_id: str | None = None) -> None:
        self.root_agent_id = root_agent_id
        self.transcript = PresentationReducer()
        self.execution = ExecutionViewReducer(root_agent_id=root_agent_id)
        self._lock = threading.RLock()
        self._invalidate = lambda: None
        self._notice_seq = 0
        self._pending_events: queue.SimpleQueue[UIEvent] = queue.SimpleQueue()

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
                if isinstance(event.payload, RuntimeEventPayload):
                    runtime = event.payload.event
                    self.transcript.apply(runtime)
                    self.execution.apply(runtime)
                elif isinstance(event.payload, InteractionPromptPayload):
                    continue
                else:
                    message = event.message
                    if isinstance(event.payload, ViewEventPayload):
                        if (
                            event.payload.view_type == "session_resume"
                            and hasattr(event.payload.view_model, "entries")
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

    def has_animation_lease(self) -> bool:
        now = time.time()
        with self._lock:
            return any(
                agent.is_animating(now) for agent in self.execution.state.agents.values()
            )

    def transcript_fragments(self) -> FormattedText:
        self._drain_pending_events()
        fragments: list[tuple[str, str]] = []
        with self._lock:
            cells = self.transcript.state.transcript.cells
        for cell in cells:
            fragments.extend(_cell_fragments(cell))
        return FormattedText(fragments or [("class:muted", "No activity yet.\n")])


class MiniTUIInteractor:
    """Blocking UIInteractor whose requests are answered by the bottom pane."""

    def __init__(self, ui_bus) -> None:
        self.ui_bus = ui_bus
        self._condition = threading.Condition()
        self._active: Any | None = None
        self._response: Any | None = None
        self._invalidate = lambda: None
        self._details_expanded = False
        self._detail_scroll = 0

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
            if isinstance(request, ReviewRequest) and text.strip().lower() == "d":
                self._details_expanded = not self._details_expanded
                self._detail_scroll = 0
                self._invalidate()
                return True
            response = _interaction_response(request, text)
            if response is None:
                return True
            self._response = response
            self._active = None
            self._details_expanded = False
            self._detail_scroll = 0
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
            self._details_expanded = False
            self._detail_scroll = 0
            self._condition.notify_all()
        self._invalidate()

    def cancel_active(self, reason: str = "interaction interrupted") -> bool:
        with self._condition:
            request = self._active
            if request is None:
                return False
            self._response = _cancelled_response(request, reason)
            self._active = None
            self._details_expanded = False
            self._detail_scroll = 0
            self._condition.notify_all()
        self._invalidate()
        return True

    @property
    def details_expanded(self) -> bool:
        with self._condition:
            return self._details_expanded

    @property
    def detail_scroll(self) -> int:
        with self._condition:
            return self._detail_scroll

    def scroll_details(self, delta: int) -> bool:
        with self._condition:
            if not isinstance(self._active, ReviewRequest) or not self._details_expanded:
                return False
            self._detail_scroll = max(0, self._detail_scroll + delta)
        self._invalidate()
        return True

    def _ask(self, request):
        with self._condition:
            if self._active is not None:
                raise RuntimeError("mini-TUI interaction slot is already occupied")
            self._active = request
            self._response = None
            self._details_expanded = False
            self._detail_scroll = 0
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
        self.transcript_control = FormattedTextControl(
            self.events.transcript_fragments,
            focusable=True,
            show_cursor=False,
        )
        self.transcript_window = Window(
            self.transcript_control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
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
            mouse_support=False,
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
            self.application.run()
        finally:
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
            if result["action"] == "continue" and (
                self.current_session_id != previous_session_id
                or self.agent.session_generation != previous_generation
            ):
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
                    self.ui_bus.warning("Cancelling at the next protocol-safe boundary…")
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
            if self.interactor.scroll_details(-5):
                return
            self.transcript_window.vertical_scroll = max(
                0, self.transcript_window.vertical_scroll - 5
            )

        @bindings.add("pagedown")
        def _page_down(event) -> None:  # noqa: ARG001
            if self.interactor.scroll_details(5):
                return
            self.transcript_window.vertical_scroll += 5

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
        lines = self._panel_lines()
        return FormattedText(
            [
                ("class:panel.header" if index == 0 else "class:panel.body", line + "\n")
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
            16,
            max(
                2,
                len(
                    _interaction_lines(
                        request,
                        expanded=self.interactor.details_expanded,
                        scroll=self.interactor.detail_scroll,
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
                        expanded=self.interactor.details_expanded,
                        scroll=self.interactor.detail_scroll,
                    )
                ]
            )
        if self.exit_confirm:
            return FormattedText(
                [("class:warning", "Press Ctrl+C again to exit; Esc keeps the session.\n")]
            )
        if self.cancelling:
            return FormattedText([("class:warning", "Cancelling safely…\n")])
        if self.running:
            return FormattedText([("class:muted", "Agent running · Ctrl+C interrupts\n")])
        return FormattedText([("class:muted", "/help for commands · PageUp/PageDown scroll\n")])

    def _input_title(self) -> str:
        return " REVIEW " if self.interactor.active_request else " YOU "

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


def _cell_fragments(cell) -> list[tuple[str, str]]:
    if isinstance(cell, UserCell):
        return [("class:user", f" YOU  {cell.text}\n")]
    if isinstance(cell, AssistantCell):
        return [("class:assistant", cell.text + ("\n" if cell.text else ""))]
    if isinstance(cell, ToolCell):
        status = cell.status.value.upper()
        style = "class:error" if cell.status.value == "failed" else "class:tool"
        text = f" {status}  {cell.name}"
        if cell.outcome is not None:
            text += f" · {cell.outcome.summary}"
        if cell.output:
            text += "\n" + "\n".join(cell.output.splitlines()[-5:])
        return [(style, text + "\n")]
    if isinstance(cell, DiffCell):
        fragments: list[tuple[str, str]] = []
        for line in cell.diff.splitlines():
            style = "class:diff.header"
            if line.startswith("+") and not line.startswith("+++"):
                style = "class:diff.add"
            elif line.startswith("-") and not line.startswith("---"):
                style = "class:diff.del"
            fragments.append((style, line + "\n"))
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
        return [("class:warning", f" APPROVAL  {cell.title} · {cell.status}\n")]
    if isinstance(cell, SubagentCell):
        return [("class:tool", f" AGENT  {cell.job_id} · {cell.status} · {cell.task}\n")]
    if isinstance(cell, DiagnosticCell):
        return [("class:warning", f" LSP  {cell.path} · {len(cell.diagnostics)} diagnostic(s)\n")]
    return [("class:muted", str(cell) + "\n")]


def _interaction_lines(
    request, *, expanded: bool = False, scroll: int = 0
) -> list[str]:
    lines = [request.title]
    if isinstance(request, ReviewRequest):
        lines.append(request.summary)
        details: list[str] = []
        for section in request.sections:
            details.append(f"[{section.title}]")
            content = section.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            detail_lines = content.splitlines()
            details.extend(detail_lines if expanded else detail_lines[:4])
        if expanded:
            visible = details[scroll : scroll + 11]
            lines.extend(visible)
            if scroll > 0:
                lines.append("↑ PageUp for earlier details")
            if scroll + 11 < len(details):
                lines.append("↓ PageDown for more details")
        else:
            lines.extend(details[:7])
        lines.append(
            f"[Enter/Y] {request.approve_label}   [D] Details   "
            f"[N] {request.reject_label}"
        )
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
        lines = [
            f"RESTORED {model.session_id} · {model.model} · {model.saved_at[:19]}"
        ]
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
