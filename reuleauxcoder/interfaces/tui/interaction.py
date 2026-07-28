"""Blocking interaction bridge owned by the production terminal UI."""

from __future__ import annotations

import threading
import time
from typing import Any

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


def interaction_lines(request) -> list[str]:
    """Render a shared interaction request as compact bottom-pane lines."""
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


def interaction_response(request, text: str):
    """Translate bottom-pane text into the matching typed response."""
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
            response = interaction_response(request, text)
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
            self._response = cancelled_response(request, "interaction cancelled")
            self._active = None
            self._condition.notify_all()
        self._invalidate()

    def cancel_active(self, reason: str = "interaction interrupted") -> bool:
        with self._condition:
            request = self._active
            if request is None:
                return False
            self._response = cancelled_response(request, reason)
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
