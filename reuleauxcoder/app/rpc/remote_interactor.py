"""Reverse UI requests own cancellation independently of frontend replies."""

from concurrent.futures import CancelledError
from dataclasses import replace
import threading
import time

from reuleauxcoder.app.interaction_contracts import cancelled_response
from reuleauxcoder.app.rpc.codec import decode, encode


class RemoteInteractor:
    def __init__(self, peer):
        self.peer = peer
        self.review_request = None
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._cancelled: dict[str, None] = {}

    def _ask(self, kind, request):
        timeout = (
            max(0.0, request.deadline - time.monotonic())
            if request.deadline is not None
            else None
        )
        wire_request = replace(request, deadline=None)
        with self._lock:
            if request.request_id in self._cancelled:
                self._cancelled.pop(request.request_id)
                return cancelled_response(request, "interaction cancelled")
            cancellation = threading.Event()
            self._active[request.request_id] = cancellation
        try:
            return decode(
                self.peer.request(
                    "interaction.request",
                    {
                        "kind": kind,
                        "request": encode(wire_request),
                        "timeout_seconds": timeout,
                    },
                    timeout=timeout,
                    cancellation_event=cancellation,
                )
            )
        except CancelledError:
            return cancelled_response(request, "interaction cancelled")
        except TimeoutError:
            self.cancel(request.request_id)
            return cancelled_response(request, "interaction deadline exceeded")
        finally:
            with self._lock:
                self._active.pop(request.request_id, None)

    def confirm(self, request):
        return self._ask("confirm", request)

    def choose_one(self, request):
        return self._ask("choose_one", request)

    def input_text(self, request):
        return self._ask("input_text", request)

    def review(self, request):
        self.review_request = request
        try:
            return self._ask("review", request)
        finally:
            self.review_request = None

    def cancel(self, request_id):
        with self._lock:
            cancellation = self._active.get(request_id)
            if cancellation is not None:
                cancellation.set()
            else:
                self._cancelled[request_id] = None
                if len(self._cancelled) > 1024:
                    self._cancelled.pop(next(iter(self._cancelled)))
        if not self.peer.closed.is_set():
            self.peer.notify("interaction.cancel", {"request_id": request_id})

    def notify(self, event):
        self.peer.notify("runtime.event", {"event": encode(event)})

