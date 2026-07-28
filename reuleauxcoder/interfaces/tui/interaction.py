"""Blocking interaction bridge owned by the production terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Literal

from reuleauxcoder.interfaces.events import UIEvent
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


ReviewStage = Literal["review", "scope", "feedback"]


@dataclass(frozen=True, slots=True)
class ReviewInteractionState:
    stage: ReviewStage = "review"
    selected_index: int = 0


def interaction_lines(
    request,
    review_state: ReviewInteractionState | None = None,
) -> list[str]:
    """Render a shared interaction request as compact bottom-pane lines."""
    lines = [request.title]
    if isinstance(request, ReviewRequest):
        state = review_state or ReviewInteractionState()
        if state.stage == "scope":
            lines = [
                "Grant for this session (also applies when this session is resumed)"
            ]
            for index, option in enumerate(request.grant_options):
                marker = "›" if index == state.selected_index else " "
                warning = " · ⚠ broader scope" if option.broad else ""
                lines.append(
                    f"{marker} [{index + 1}] {option.label} · "
                    f"{option.description}{warning}"
                )
            lines.append(
                "Future matching calls run without another approval. "
                "[Enter] Grant   [Esc] Back"
            )
        elif state.stage == "feedback":
            lines = [
                f"Deny {request.context.tool_name if request.context else 'tool call'} "
                "and tell the model what to do differently",
                "[Enter] Deny and send feedback   [Esc] Back",
            ]
        else:
            session = (
                "   [S] Allow for session…" if request.grant_options else ""
            )
            lines = [
                f"[Enter/Y] {request.approve_label}{session}   "
                f"[N] {request.reject_label}   [F] Deny with feedback"
            ]
        if request.queue_status.waiting:
            lines.append(
                f"1 active · {request.queue_status.waiting} waiting"
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


def interaction_response(
    request,
    text: str,
    review_state: ReviewInteractionState | None = None,
):
    """Translate bottom-pane text into the matching typed response."""
    answer = text.strip().lower()
    if isinstance(request, ReviewRequest):
        state = review_state or ReviewInteractionState()
        if state.stage == "scope":
            if not request.grant_options:
                return None
            selected_index = state.selected_index
            if answer.isdigit():
                selected_index = int(answer) - 1
            if answer and not answer.isdigit():
                return None
            if 0 <= selected_index < len(request.grant_options):
                return ReviewResponse(
                    True,
                    action="allow_session",
                    selected_id=request.grant_options[selected_index].id,
                )
            return None
        if state.stage == "feedback":
            feedback = text.strip()
            if not feedback:
                return None
            return ReviewResponse(
                False,
                reason=feedback,
                action="deny",
            )
        if answer in {"", "1", "y", "yes"}:
            return ReviewResponse(True, action="allow_once")
        if answer in {"2", "n", "no"}:
            return ReviewResponse(False, action="deny")
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


def cancelled_response(request, reason: str):
    """Build the request-specific cancellation response."""
    if isinstance(request, ReviewRequest):
        return ReviewResponse(False, cancelled=True, reason=reason)
    if isinstance(request, ConfirmRequest):
        return ConfirmResponse(False, cancelled=True)
    if isinstance(request, ChooseOneRequest):
        return ChooseOneResponse(None, cancelled=True)
    return InputTextResponse(None, cancelled=True)


class MiniTUIInteractor:
    """Blocking UIInteractor whose requests are answered by the bottom pane."""

    def __init__(self, ui_bus) -> None:
        self.ui_bus = ui_bus
        self._condition = threading.Condition()
        self._active: Any | None = None
        self._response: Any = None
        self._invalidate = lambda: None
        self._review_state = ReviewInteractionState()

    @property
    def active_request(self):
        with self._condition:
            return self._active

    @property
    def review_state(self) -> ReviewInteractionState:
        with self._condition:
            return self._review_state

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
        transitioned = False
        resolved = False
        with self._condition:
            request = self._active
            if request is None:
                return False
            answer = text.strip().lower()
            if isinstance(request, ReviewRequest):
                if (
                    self._review_state.stage == "review"
                    and answer == "s"
                    and request.grant_options
                ):
                    self._review_state = ReviewInteractionState("scope", 0)
                    transitioned = True
                elif self._review_state.stage == "review" and answer == "f":
                    self._review_state = ReviewInteractionState("feedback", 0)
                    transitioned = True
            if transitioned:
                response = None
            else:
                response = interaction_response(
                    request,
                    text,
                    self._review_state,
                )
            if response is None:
                pass
            else:
                self._response = response
                self._active = None
                self._review_state = ReviewInteractionState()
                self._condition.notify_all()
                resolved = True
        if transitioned or resolved:
            self._invalidate()
        return True

    def move_review_selection(self, delta: int) -> bool:
        with self._condition:
            request = self._active
            if (
                not isinstance(request, ReviewRequest)
                or self._review_state.stage != "scope"
                or not request.grant_options
            ):
                return False
            index = (
                self._review_state.selected_index + delta
            ) % len(request.grant_options)
            self._review_state = ReviewInteractionState("scope", index)
        self._invalidate()
        return True

    def back_review(self) -> bool:
        with self._condition:
            if (
                not isinstance(self._active, ReviewRequest)
                or self._review_state.stage == "review"
            ):
                return False
            self._review_state = ReviewInteractionState()
        self._invalidate()
        return True

    def cancel(self, request_id: str) -> None:
        with self._condition:
            request = self._active
            if request is None or request.request_id != request_id:
                return
            self._response = cancelled_response(request, "interaction cancelled")
            self._active = None
            self._review_state = ReviewInteractionState()
            self._condition.notify_all()
        self._invalidate()

    def cancel_active(self, reason: str = "interaction interrupted") -> bool:
        with self._condition:
            request = self._active
            if request is None:
                return False
            self._response = cancelled_response(request, reason)
            self._active = None
            self._review_state = ReviewInteractionState()
            self._condition.notify_all()
        self._invalidate()
        return True

    def _ask(self, request):
        with self._condition:
            if self._active is not None:
                raise RuntimeError("mini-TUI interaction slot is already occupied")
            self._active = request
            self._response = None
            self._review_state = ReviewInteractionState()
        self.ui_bus.emit_interaction_prompt(request)
        self._invalidate()
        with self._condition:
            while self._active is request:
                timeout = 0.1
                if request.deadline is not None:
                    remaining = request.deadline - time.monotonic()
                    if remaining <= 0:
                        self._active = None
                        self._review_state = ReviewInteractionState()
                        self._response = cancelled_response(
                            request, "interaction deadline exceeded"
                        )
                        break
                    timeout = min(timeout, remaining)
                self._condition.wait(timeout)
            response = self._response
            self._response = None
            return response


# Compatibility names used by the former monolithic module.
_interaction_lines = interaction_lines
_interaction_response = interaction_response
_cancelled_response = cancelled_response

__all__ = [
    "MiniTUIInteractor",
    "cancelled_response",
    "interaction_lines",
    "interaction_response",
]
