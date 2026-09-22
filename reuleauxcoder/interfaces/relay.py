"""Remote terminal adapter. Execution and persistence cross JSON-RPC."""

from __future__ import annotations

import io
import threading

from rich.console import Console

from reuleauxcoder.app.interaction_contracts import (
    ChooseOneRequest,
    ChooseOneResponse,
    ConfirmRequest,
    ConfirmResponse,
    InputTextRequest,
    InputTextResponse,
    ReviewRequest,
    ReviewResponse,
)
from reuleauxcoder.app.ui_events import UIEventBus, RuntimeEventPayload
from reuleauxcoder.domain.runtime.events import ErrorOccurred, UserSteeringApplied
from reuleauxcoder.extensions.remote_exec.protocol import (
    TerminalCapabilities,
    ChatResponse,
)
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.interaction_presenter import (
    interaction_constraints,
    render_interaction_request,
)
from reuleauxcoder.presentation import PresentationPolicy


def create_remote_console(terminal: TerminalCapabilities) -> Console:
    """Create the Host renderer sink from negotiated peer terminal facts."""
    color_system = {
        "none": None,
        "standard": "standard",
        "256": "256",
        "truecolor": "truecolor",
    }[terminal.color_level]
    return Console(
        file=io.StringIO(),
        record=True,
        width=terminal.width,
        # This is a record-only Host sink, never the Host's real terminal.
        # ANSI is added explicitly by export_remote_console when supported.
        force_terminal=False,
        force_jupyter=False,
        color_system=color_system,
        emoji=terminal.unicode,
    )


def export_remote_console(console: Console, *, clear: bool = True) -> str:
    """Export ANSI only when the peer declared color support."""
    return console.export_text(
        clear=clear,
        styles=console.color_system is not None,
    )


class RelayUI:
    """One persistent terminal view; individual HTTP chat streams can come and go."""

    def __init__(self, terminal):
        self.terminal = terminal
        self.console = create_remote_console(terminal())
        self.bus = UIEventBus()
        self.session = None
        self._lock = threading.RLock()
        self.result = None
        self._interaction_sessions = {}

    def connect(self, runtime):
        self.runtime = runtime
        self.renderer = CLIRenderer(
            console_override=self.console,
            policy=PresentationPolicy.from_mapping(runtime.info["presentation"]),
            root_agent_id=runtime.state.agent_id,
            terminal_width_provider=lambda: self.terminal().width,
        )
        self.bus.subscribe(self.notify, replay_history=False)
        self.bus.bind_subscriber_failure_sink(
            runtime.report_runtime_issue,
            agent_id=runtime.state.agent_id,
            default=True,
        )
        runtime.on_completed = self._completed
        runtime.peer.notifications["relay.tool_stream"] = self._tool_stream

    def _completed(self, result):
        self.result = result

    def _tool_stream(self, **payload):
        if self.session is not None:
            self.session.append_event("tool_call_stream", payload)

    def flush(self):
        with self._lock:
            rendered = export_remote_console(self.console)
            if rendered and self.session is not None:
                self.session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

    def notify(self, event):
        with self._lock:
            if self.session is not None and isinstance(
                event.payload, RuntimeEventPayload
            ):
                payload = event.payload.event.payload
                if isinstance(payload, ErrorOccurred):
                    self.session.append_event("error", {"message": payload.message})
                elif isinstance(payload, UserSteeringApplied):
                    self.session.append_event(
                        "steering_applied",
                        {
                            "steering_id": payload.steering_id,
                            "attempt_id": payload.attempt_id,
                        },
                    )
            self.renderer.on_ui_event(event)
            self.flush()

    def run(self, prompt, session=None):
        try:
            return self._run(prompt, session)
        finally:
            self.session = None

    def _run(self, prompt, session):
        self.session = session
        self.result = None
        error = None
        persistence_error = None
        try:
            self.runtime.submit(
                prompt.strip() if prompt.strip().startswith("/") else prompt
            )
            if session is not None:
                session.cancel_callback = self.runtime.stop
                session.bind_chat_control(
                    admit_steering=self.runtime.admit_steering,
                    interrupt_intent=lambda: self.runtime.interrupt()["outcome"],
                    stop_turn=self.runtime.stop,
                )
            self.runtime.ready()
            self.runtime.wait_idle()
        except Exception as failure:
            error = str(failure)
        finally:
            self.flush()
            persistence_error = self.runtime.peer.request("runtime.checkpoint")
        try:
            if session is not None:
                if self.result is not None and self.result.control == "exit":
                    session.append_event(
                        "output",
                        {
                            "format": "plain",
                            "content": "Exit command received. Use Ctrl+C to terminate remote peer.\n",
                        },
                    )
                if error:
                    session.append_event("error", {"message": error})
                if persistence_error:
                    session.append_event("error", persistence_error)
                session.append_event(
                    "chat_end",
                    {"response": self.result.response or "" if self.result else ""},
                )
            if persistence_error:
                error = "\n".join(
                    item for item in (error, persistence_error["message"]) if item
                )
            return ChatResponse(
                response=self.result.response or "" if self.result else "", error=error
            )
        finally:
            self.session = None

    def close(self):
        self.bus.unsubscribe(self.notify)
        self.renderer.close()

    def _request(self, request):
        session = self.session
        if session is None:
            raise RuntimeError("Interactions require a streaming remote chat")
        self._interaction_sessions[request.request_id] = session
        session.register_interaction(request.request_id)
        with self._lock:
            self.flush()
            render_interaction_request(
                self.console,
                request,
                max_preview_lines=self.renderer.policy.tool_preview_lines,
                max_preview_chars=self.renderer.policy.tool_preview_chars,
            )
            rendered_frame = export_remote_console(self.console)
        kind = {
            ConfirmRequest: "confirm",
            ChooseOneRequest: "choose_one",
            InputTextRequest: "text_input",
            ReviewRequest: "review",
        }[type(request)]
        payload = {
            "request_id": request.request_id,
            "kind": kind,
            "rendered_frame": rendered_frame,
            "input_constraints": interaction_constraints(request),
        }
        session.append_event("interaction_request", payload)
        try:
            value, cancelled, reason = session.wait_interaction(request.request_id)
            session.append_event(
                "interaction_resolved",
                {
                    "request_id": request.request_id,
                    "cancelled": cancelled,
                    "reason": reason,
                },
            )
            return value, cancelled, reason
        finally:
            self._interaction_sessions.pop(request.request_id, None)

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        value, cancelled, _ = self._request(request)
        return ConfirmResponse(confirmed=value is True, cancelled=cancelled)

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        value, cancelled, _ = self._request(request)
        selected = value if isinstance(value, str) else None
        return ChooseOneResponse(selected_id=selected, cancelled=cancelled)

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        value, cancelled, _ = self._request(request)
        text = value if isinstance(value, str) else None
        return InputTextResponse(value=text, cancelled=cancelled)

    def review(self, request: ReviewRequest) -> ReviewResponse:
        value, cancelled, reason = self._request(request)
        if isinstance(value, dict):
            action = value.get("action")
            selected_id = value.get("selected_id")
            feedback = value.get("reason")
            if action in {"allow_once", "allow_session", "deny"}:
                return ReviewResponse(
                    approved=action in {"allow_once", "allow_session"}
                    and not cancelled,
                    cancelled=cancelled,
                    reason=(str(feedback) if isinstance(feedback, str) else reason),
                    action=action,
                    selected_id=(
                        str(selected_id) if isinstance(selected_id, str) else None
                    ),
                )
        return ReviewResponse(
            approved=value is True and not cancelled,
            cancelled=cancelled,
            reason=reason,
        )

    def cancel(self, request_id: str) -> None:
        session = self._interaction_sessions.get(request_id)
        if session is not None:
            session.resolve_interaction(request_id, None, True, "interaction cancelled")
