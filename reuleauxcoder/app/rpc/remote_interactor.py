"""Reverse UI requests own cancellation independently of frontend replies."""

from concurrent.futures import CancelledError
from dataclasses import replace
import threading
import time

from reuleauxcoder.app.interaction_contracts import cancelled_response
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.infrastructure.rpc.peer import RpcError


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
        if kind == "review":
            wire_request = replace(wire_request, documents=tuple(
                replace(document, before=None, after=None)
                for document in request.documents
            ))
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
        with self._lock:
            self.review_request = request
        try:
            return self._ask("review", request)
        finally:
            with self._lock:
                self.review_request = None

    def document(self, request_id, document_id, side, offset=0, limit=65536):
        """Read the active proposal only, never a frontend-supplied file path."""
        if (
            side not in {"before", "after"}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 65536
        ):
            raise RpcError(-32602, "Invalid review document page")
        with self._lock:
            request = self.review_request
            cancellation = self._active.get(request_id)
            if (
                request is None or request.request_id != request_id
                or cancellation is None or cancellation.is_set()
            ):
                raise RpcError(-32002, "Review is no longer active")
            document = next((item for item in request.documents if item.id == document_id), None)
            if document is None:
                raise RpcError(-32602, "Unknown review document")
            content = getattr(document, side)
            if content is None or offset > len(content):
                raise RpcError(-32602, "Invalid review document offset")
            end = min(len(content), offset + limit)
            return {"text": content[offset:end], "next_offset": end, "complete": end == len(content)}

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
