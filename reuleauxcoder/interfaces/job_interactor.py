"""File-based unattended interaction adapter; never reads terminal stdin."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from reuleauxcoder.app.interaction_contracts import (
    ChooseOneResponse,
    ConfirmResponse,
    InputTextResponse,
    ReviewResponse,
    cancelled_response,
)
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.infrastructure.persistence.job_store import (
    LeaseBusy,
    atomic_json,
    lease,
    read_json,
)


def answer_response(kind, request, value):
    """Validate external input rather than relying on dataclass annotations."""
    if not isinstance(value, dict):
        raise TypeError("Answer must be a JSON object")
    if kind == "review":
        if (
            set(value) - {"approved", "reason"}
            or type(value.get("approved")) is not bool
        ):
            raise ValueError(
                "Review answer requires approved: true/false and optional reason"
            )
        reason = value.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Review reason must be text")
        return ReviewResponse(value["approved"], reason=reason)
    if kind == "confirm":
        if set(value) != {"confirmed"} or type(value["confirmed"]) is not bool:
            raise ValueError("Confirmation requires confirmed: true/false")
        return ConfirmResponse(value["confirmed"])
    if kind == "choose_one":
        if set(value) != {"selected_id"} or value["selected_id"] not in {
            item.id for item in request.items
        }:
            raise ValueError("Select one of the advertised choice IDs")
        return ChooseOneResponse(value["selected_id"])
    if kind == "input_text":
        if set(value) != {"value"} or not isinstance(value["value"], str):
            raise ValueError("Text answer requires a string value")
        if not request.allow_empty and not value["value"].strip():
            raise ValueError("Answer cannot be empty")
        return InputTextResponse(value["value"])
    raise ValueError("Unknown interaction kind")


class JobInteractor:
    def __init__(self, journal, attempt_id):
        self.journal = journal
        self.attempt_id = attempt_id
        self._lock = threading.Lock()
        self._pending = {}
        self.client = None

    @property
    def waiting(self):
        with self._lock:
            return bool(self._pending)

    def notify(self, event):
        self.journal.event("ui", encode(event))

    def cancel(self, request_id):
        with self._lock:
            if request_id in self._pending:
                self._pending[request_id].set()

    def _request(self, kind, request):
        request_path = self.journal.path / "requests" / f"{request.request_id}.json"
        answer_path = self.journal.path / "answers" / f"{request.request_id}.json"
        cancelled = threading.Event()
        with self._lock:
            self._pending[request.request_id] = cancelled
        try:
            value = {
                "attempt_id": self.attempt_id,
                "kind": kind,
                "request": encode(request),
            }
            atomic_json(request_path, value)
            self.journal.event(
                "interaction_requested",
                {"kind": kind, "request_id": request.request_id},
            )
            while not cancelled.wait(0.1):
                if (
                    request.deadline is not None
                    and time.monotonic() >= request.deadline
                ):
                    return cancelled_response(request, "interaction deadline exceeded")
                if answer_path.exists():
                    try:
                        with lease(self.journal.path / "control.lock"):
                            control = self.journal.path / "control.json"
                            if cancelled.is_set() or (
                                control.exists()
                                and read_json(control).get("attempt_id")
                                == self.attempt_id
                            ):
                                return cancelled_response(request, "job stopped")
                            answer = read_json(answer_path)
                            if answer.get("attempt_id") == self.attempt_id:
                                answer_response(kind, request, answer["response"])
                                request_path.unlink(missing_ok=True)
                            answer_path.unlink(missing_ok=True)
                    except LeaseBusy:
                        continue
                    except (ValueError, TypeError, KeyError) as error:
                        answer_path.unlink(missing_ok=True)
                        self.journal.event(
                            "answer_rejected",
                            {"request_id": request.request_id, "message": str(error)},
                        )
                        continue
                    if answer.get("attempt_id") != self.attempt_id:
                        continue
                    try:
                        response = answer_response(kind, request, answer["response"])
                    except (ValueError, TypeError, KeyError) as error:
                        self.journal.event(
                            "answer_rejected",
                            {"request_id": request.request_id, "message": str(error)},
                        )
                        continue
                    self.journal.event(
                        "interaction_answered", {"request_id": request.request_id}
                    )
                    return response
            return cancelled_response(request, "job stopped")
        finally:
            with lease(self.journal.path / "control.lock", wait=True):
                request_path.unlink(missing_ok=True)
                answer_path.unlink(missing_ok=True)
            with self._lock:
                self._pending.pop(request.request_id, None)

    def confirm(self, request):
        return self._request("confirm", request)

    def choose_one(self, request):
        return self._request("choose_one", request)

    def input_text(self, request):
        return self._request("input_text", request)

    def review(self, request):
        # Preserve frozen previews while the runtime still owns the request.
        if self.client is not None:
            documents = []
            for document in request.documents:
                content = {}
                for side in ("before", "after"):
                    if side == "before" and not document.before_exists:
                        content[side] = ""
                        continue
                    chunks, offset = [], 0
                    while True:
                        page = self.client.peer.request(
                            "review.document",
                            {
                                "request_id": request.request_id,
                                "document_id": document.id,
                                "side": side,
                                "offset": offset,
                            },
                        )
                        # The RPC document contract supplies bounded text pages.
                        if not page or not page.get("text"):
                            break
                        chunks.append(page["text"])
                        offset += len(page["text"])
                        if page["complete"]:
                            break
                        if offset >= 4 * 1024 * 1024:
                            return ReviewResponse(
                                False,
                                reason="Review document exceeds unattended preview limit",
                            )
                    content[side] = "".join(chunks)
                documents.append(replace(document, **content))
            request = replace(request, documents=tuple(documents))
        return self._request("review", request)


def validate_stored_answer(pending, value):
    answer_response(pending["kind"], decode(pending["request"]), value)
