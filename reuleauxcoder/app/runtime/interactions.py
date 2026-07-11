"""Shared serialization, cancellation and deadline semantics for UI input."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

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
    UIInteractor,
)

ResponseT = TypeVar("ResponseT")


class InteractionCoordinator:
    """Ensure at most one foreground interaction owns an interface.

    Deadlines are monotonic absolute timestamps. Cancellation is cooperative:
    adapters may implement ``cancel(request_id)`` to interrupt an active modal;
    otherwise the response is converted to cancelled when the adapter returns.
    """

    def __init__(self, adapter: UIInteractor):
        self.adapter = adapter
        self._slot = threading.Lock()
        self._state_lock = threading.Lock()
        self._cancellations: dict[str, threading.Event] = {}
        self._active_request_id: str | None = None

    @property
    def active_request_id(self) -> str | None:
        with self._state_lock:
            return self._active_request_id

    def cancel(self, request_id: str) -> bool:
        with self._state_lock:
            cancellation = self._cancellations.get(request_id)
            active = self._active_request_id == request_id
        if cancellation is None:
            return False
        cancellation.set()
        if active:
            cancel_adapter = getattr(self.adapter, "cancel", None)
            if callable(cancel_adapter):
                cancel_adapter(request_id)
        return True

    def notify(self, event: UIEvent) -> None:
        self.adapter.notify(event)

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        return self._invoke(
            request,
            lambda: self.adapter.confirm(request),
            lambda reason: ConfirmResponse(
                confirmed=False, cancelled=True
            ),
        )

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        return self._invoke(
            request,
            lambda: self.adapter.choose_one(request),
            lambda reason: ChooseOneResponse(selected_id=None, cancelled=True),
        )

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        return self._invoke(
            request,
            lambda: self.adapter.input_text(request),
            lambda reason: InputTextResponse(value=None, cancelled=True),
        )

    def review(self, request: ReviewRequest) -> ReviewResponse:
        return self._invoke(
            request,
            lambda: self.adapter.review(request),
            lambda reason: ReviewResponse(
                approved=False, cancelled=True, reason=reason
            ),
        )

    def _invoke(
        self,
        request: Any,
        invoke: Callable[[], ResponseT],
        cancelled: Callable[[str], ResponseT],
    ) -> ResponseT:
        request_id = request.request_id
        cancellation = threading.Event()
        with self._state_lock:
            if request_id in self._cancellations:
                raise ValueError(f"Duplicate interaction request id: {request_id}")
            self._cancellations[request_id] = cancellation

        acquired = False
        try:
            while not acquired:
                reason = self._cancel_reason(request.deadline, cancellation)
                if reason is not None:
                    return cancelled(reason)
                timeout = 0.05
                if request.deadline is not None:
                    timeout = min(timeout, max(0.0, request.deadline - time.monotonic()))
                acquired = self._slot.acquire(timeout=timeout)

            reason = self._cancel_reason(request.deadline, cancellation)
            if reason is not None:
                return cancelled(reason)
            with self._state_lock:
                self._active_request_id = request_id
            response = invoke()
            reason = self._cancel_reason(request.deadline, cancellation)
            return cancelled(reason) if reason is not None else response
        finally:
            with self._state_lock:
                if self._active_request_id == request_id:
                    self._active_request_id = None
                self._cancellations.pop(request_id, None)
            if acquired:
                self._slot.release()

    @staticmethod
    def _cancel_reason(
        deadline: float | None, cancellation: threading.Event
    ) -> str | None:
        if cancellation.is_set():
            return "interaction cancelled"
        if deadline is not None and time.monotonic() >= deadline:
            return "interaction deadline exceeded"
        return None
