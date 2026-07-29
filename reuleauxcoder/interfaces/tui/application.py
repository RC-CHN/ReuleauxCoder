"""Prompt Toolkit layout and lifecycle orchestration for the production TUI."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
import time
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.layout import (
    BufferControl,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.data_structures import Point
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame

from reuleauxcoder import __version__
from reuleauxcoder.app.commands import parse_command
from reuleauxcoder.app.commands.panels import CommandPanelRegistry
from reuleauxcoder.app.commands.specs import DuringTurnPolicy
from reuleauxcoder.app.runtime.session_state import (
    build_session_persistence_kwargs,
    build_session_runtime_state,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.domain.runtime.events import (
    AssistantStreamInterrupted,
    RuntimeEvent,
    UserSteeringApplied,
)
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.tui.command_popup import (
    PopupEntry,
    build_popup_entries,
    filter_entries,
)
from reuleauxcoder.interfaces.tui.selection_host import SelectionHost
from reuleauxcoder.interfaces.tui.style import (
    ALTERNATE_SCROLL_DISABLE,
    ALTERNATE_SCROLL_ENABLE,
    MINI_TUI_MOUSE_SUPPORT,
    MINI_TUI_STYLE,
)
from reuleauxcoder.interfaces.tui.virtual_transcript import VirtualTranscriptControl
from reuleauxcoder.interfaces.events import (
    UIEvent,
    UIEventKind,
)
from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ConfirmRequest,
    InputTextRequest,
    ReviewRequest,
)
from reuleauxcoder.interfaces.tui.interaction import (
    MiniTUIInteractor,
    interaction_lines as _interaction_lines,
)
from reuleauxcoder.interfaces.tui.input_router import build_key_bindings
from reuleauxcoder.interfaces.tui.formatting import (
    clip as _clip,
    fit_styled_row as _fit_styled_row,
    wrapped_row_count as _wrapped_row_count,
)
from reuleauxcoder.interfaces.tui.execution_panel import _execution_panel_rows
from reuleauxcoder.interfaces.tui.event_adapter import MiniTUIEventAdapter




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
        self.round_interrupt_applying = False
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
        self._last_terminal_columns = 0
        self.session_header_expanded = True
        self.startup_lines = tuple(
            event.message.splitlines()[0]
            for event in startup_events
            if event.message.strip() and event.kind is not UIEventKind.MCP
        )[:6]
        self.events.runtime_event_handler = self._on_runtime_event

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
        self.interaction_input_buffer = Buffer(
            multiline=False,
            accept_handler=self._accept_interaction_buffer,
        )
        self._interaction_input_owner: tuple[str, str] | None = None
        self.panel_control = FormattedTextControl(self._panel_text)
        self._popup_entries: tuple[PopupEntry, ...] = build_popup_entries(
            action_registry, ui_profile
        )
        self._popup_index = 0
        self._popup_last_text = ""
        self._popup_dismissed = False
        self.selection_host = SelectionHost(
            registry=panel_registry,
            input_text=lambda: self.input_buffer.text,
            submit_command=self._submit_panel_command,
            invalidate=self.invalidate,
        )
        self.events.interactive_view_handler = self.selection_host.open_view
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
        self.selection_control = FormattedTextControl(self.selection_host.text)
        self.selection_window = Window(
            self.selection_control,
            height=self.selection_host.height,
            style="class:popup",
        )
        self.input_window = Window(
            BufferControl(
                buffer=self.input_buffer,
            ),
            height=self._input_height,
            wrap_lines=True,
            style="class:input",
        )
        self.interaction_input_window = Window(
            BufferControl(
                buffer=self.interaction_input_buffer,
                input_processors=[
                    ConditionalProcessor(
                        PasswordProcessor(char="•"),
                        Condition(self._secret_input_active),
                    )
                ],
            ),
            height=self._interaction_input_height,
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
                            self.interaction_input_window,
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
            key_bindings=build_key_bindings(self),
            full_screen=True,
            style=MINI_TUI_STYLE,
            mouse_support=MINI_TUI_MOUSE_SUPPORT,
            min_redraw_interval=1 / 30,
            max_render_postpone_time=0.05,
            before_render=self._before_render,
        )
        self.events.bind_invalidator(self.invalidate)
        self.interactor.bind_invalidator(self._interaction_changed)

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
        raw_text = buffer.text
        text = raw_text.strip()
        active_request = self.interactor.active_request
        if active_request is not None:
            # Binary approvals borrow the input focus but not its contents.
            # The buffer may contain a chat draft from before the request
            # arrived; Enter accepts the advertised default without consuming
            # that draft, just like the dedicated Y / N bindings below.
            if isinstance(active_request, (ConfirmRequest, ReviewRequest)):
                self.interactor.submit("")
                return True
            # Choice/text prompts own a separate transient interaction buffer;
            # never consume a chat draft if focus briefly lags a state change.
            return True
        if not text:
            buffer.reset()
            return True
        if self.running:
            accepted = self._submit_during_turn(text)
            if accepted:
                buffer.reset()
            self.invalidate()
            return True
        buffer.reset()
        self.exit_confirm = False
        self.session_header_expanded = False
        # A stop request belongs to the turn that just finished.  Local slash
        # commands do not pass through Agent.chat(), so clear the stale flag at
        # the same idle-operation boundary before starting either kind of input.
        self.agent.clear_stop_request()
        self.running = True
        self.cancelling = False
        self.round_interrupt_applying = False
        self._worker = threading.Thread(
            target=self._handle_input,
            args=(text,),
            name="rcoder-cli-turn",
            daemon=True,
        )
        self._worker.start()
        self.invalidate()
        return True

    def _accept_interaction_buffer(self, buffer: Buffer) -> bool:
        request = self.interactor.active_request
        if request is None:
            buffer.reset()
            return True
        raw_text = buffer.text
        self.interactor.submit(
            raw_text if isinstance(request, InputTextRequest) else raw_text.strip()
        )
        if self.interactor.active_request is not request:
            buffer.reset()
        return True

    def _secret_input_active(self) -> bool:
        request = self.interactor.active_request
        return isinstance(request, InputTextRequest) and request.secret

    def _interaction_input_active(self) -> bool:
        request = self.interactor.active_request
        if isinstance(request, (ChooseOneRequest, InputTextRequest)):
            return True
        return (
            isinstance(request, ReviewRequest)
            and self.interactor.review_state.stage == "feedback"
        )

    def _interaction_input_height(self) -> int:
        return 1 if self._interaction_input_active() else 0

    def _interaction_changed(self) -> None:
        request = self.interactor.active_request
        stage = (
            self.interactor.review_state.stage
            if isinstance(request, ReviewRequest)
            else "input"
        )
        owner = (
            (request.request_id, stage)
            if request is not None and self._interaction_input_active()
            else None
        )
        if owner != self._interaction_input_owner:
            self.interaction_input_buffer.reset()
            self._interaction_input_owner = owner
        try:
            target = (
                self.interaction_input_window
                if owner is not None
                else self.input_window
            )
            self.application.layout.focus(target)
        except (RuntimeError, ValueError):
            pass
        self.invalidate()

    def _submit_panel_command(self, command: str) -> None:
        self.input_buffer.text = command
        self.input_buffer.cursor_position = len(command)
        self._accept_buffer(self.input_buffer)

    def _submit_during_turn(self, text: str) -> bool:
        """Route active-turn input without leaking slash commands to the model."""
        if not text.startswith("/"):
            # Queued steering hangs above the input lane as a preview and only
            # enters the transcript when the agent injects it (drain event).
            return bool(self.agent.submit_user_steering(text))

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
            return True

        thread = threading.Thread(
            target=self._handle_concurrent_command,
            args=(text,),
            name="rcoder-cli-command",
            daemon=True,
        )
        thread.start()
        return True

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        """Mirror only presentation timing; Agent remains interrupt authority."""
        if isinstance(event.payload, AssistantStreamInterrupted):
            if self.agent.round_interrupt_pending():
                self.round_interrupt_applying = True
        elif isinstance(event.payload, UserSteeringApplied):
            self.round_interrupt_applying = False

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
            stop_requested = getattr(self.agent, "stop_requested", None)
            was_cancelling = (
                bool(stop_requested()) if callable(stop_requested) else self.cancelling
            )
            self.cancelling = False
            self.round_interrupt_applying = False
            if was_cancelling:
                process_manager = getattr(self.agent, "process_manager", None)
                active_processes = (
                    process_manager.active_count(
                        owner_session_id=self.current_session_id
                    )
                    if process_manager is not None
                    else 0
                )
                process_note = (
                    f" {active_processes} process session(s) remain unresolved; "
                    "use /ps to inspect or /stop to control them."
                    if active_processes
                    else ""
                )
                self.ui_bus.info(
                    "Current turn cancelled." + process_note,
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
        if self.selection_host.active and not self.selection_host.filterable:
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
        if self.interactor.active_request is not None or self.selection_host.active:
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
                        self.interactor.review_state,
                    )
                ),
            ),
        )

    def _interaction_text(self) -> FormattedText:
        request = self.interactor.active_request
        if request is not None:
            return FormattedText(
                [
                    (
                        "class:warning" if "⚠" in line else "class:interaction",
                        line + "\n",
                    )
                    for line in _interaction_lines(
                        request,
                        self.interactor.review_state,
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
            round_interrupt_pending = bool(
                getattr(self.agent, "round_interrupt_pending", lambda: False)()
            )
            if round_interrupt_pending:
                status = (
                    "Applying queued steering…"
                    if self.round_interrupt_applying
                    else "Interrupting the current request…"
                )
                return FormattedText(
                    [
                        ("class:warning", status + "\n"),
                        (
                            "class:muted",
                            "Press Ctrl+C again to discard it and cancel the turn\n",
                        ),
                    ]
                )
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
                hint = (
                    "Ctrl+C applies steering now; commands still run when idle\n"
                )
            elif queued_commands:
                hint = "Ctrl+C cancels the turn and runs queued commands next\n"
            elif queued_steering:
                hint = "Ctrl+C interrupts the request and applies queued steering\n"
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
            resized = (
                size.rows != self._last_terminal_rows
                or size.columns
                != getattr(self, "_last_terminal_columns", size.columns)
            )
            if resized:
                self._sync_process_terminal_size(size.rows, size.columns)
            viewport = max(
                1,
                (
                    size.rows - self._panel_height() - self._interaction_height() - 6
                    if resized or not self.transcript_control.last_height
                    else self.transcript_control.last_height
                ),
            )
            self._last_terminal_rows = size.rows
            self._last_terminal_columns = size.columns
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

    def _sync_process_terminal_size(self, rows: int, columns: int) -> None:
        manager = getattr(self.agent, "process_manager", None)
        resize = getattr(manager, "resize_tty_sessions", None)
        if not callable(resize):
            return
        resize(
            rows=max(1, rows),
            columns=max(1, columns),
            agent_id=str(self.agent.agent_id),
            owner_session_id=self.current_session_id,
            session_generation=int(self.agent.session_generation),
        )

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
        discard_steering = getattr(
            self.agent, "discard_pending_user_steering", None
        )
        if callable(discard_steering):
            discard_steering(reason="session_exit")
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
