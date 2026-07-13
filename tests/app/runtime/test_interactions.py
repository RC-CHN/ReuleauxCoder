from __future__ import annotations

import threading
import time

from reuleauxcoder.app.runtime.interactions import InteractionCoordinator
from reuleauxcoder.interfaces.interactions import (
    ChooseOneResponse,
    ConfirmRequest,
    ConfirmResponse,
    InputTextResponse,
    ReviewRequest,
    ReviewResponse,
)


class _Adapter:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.cancelled = []

    def notify(self, event) -> None:
        pass

    def confirm(self, request) -> ConfirmResponse:
        self.calls.append(request.request_id)
        self.entered.set()
        self.release.wait(timeout=2)
        return ConfirmResponse(confirmed=True)

    def choose_one(self, request) -> ChooseOneResponse:
        raise AssertionError("not expected")

    def input_text(self, request) -> InputTextResponse:
        raise AssertionError("not expected")

    def review(self, request) -> ReviewResponse:
        self.calls.append(request.request_id)
        return ReviewResponse(approved=True)

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)


def test_waiting_interaction_deadline_does_not_enter_adapter() -> None:
    adapter = _Adapter()
    coordinator = InteractionCoordinator(adapter)
    first = ConfirmRequest(title="first", message="first")
    first_result = []
    thread = threading.Thread(
        target=lambda: first_result.append(coordinator.confirm(first))
    )
    thread.start()
    assert adapter.entered.wait(timeout=1)

    second = ReviewRequest(
        title="second",
        summary="second",
        deadline=time.monotonic() + 0.05,
    )
    second_result = coordinator.review(second)

    assert second_result.cancelled is True
    assert second_result.reason == "interaction deadline exceeded"
    assert adapter.calls == [first.request_id]
    adapter.release.set()
    thread.join(timeout=1)
    assert first_result[0].confirmed is True


def test_cancel_active_interaction_notifies_adapter_and_denies_response() -> None:
    adapter = _Adapter()
    coordinator = InteractionCoordinator(adapter)
    request = ConfirmRequest(title="confirm", message="confirm")
    results = []
    thread = threading.Thread(
        target=lambda: results.append(coordinator.confirm(request))
    )
    thread.start()
    assert adapter.entered.wait(timeout=1)
    assert coordinator.active_request_id == request.request_id

    assert coordinator.cancel(request.request_id) is True
    adapter.release.set()
    thread.join(timeout=1)

    assert adapter.cancelled == [request.request_id]
    assert results[0].confirmed is False
    assert results[0].cancelled is True
    assert coordinator.active_request_id is None


def test_interactions_are_serialized_in_call_order() -> None:
    adapter = _Adapter()
    adapter.release.set()
    coordinator = InteractionCoordinator(adapter)
    first = ConfirmRequest(title="first", message="first")
    second = ReviewRequest(title="second", summary="second")

    assert coordinator.confirm(first).confirmed is True
    assert coordinator.review(second).approved is True
    assert adapter.calls == [first.request_id, second.request_id]


def test_shutdown_cancels_active_and_waiting_requests_and_rejects_new_ones() -> None:
    adapter = _Adapter()
    coordinator = InteractionCoordinator(adapter)
    active = ConfirmRequest(title="active", message="active")
    waiting = ReviewRequest(title="waiting", summary="waiting")
    active_results = []
    waiting_results = []
    active_thread = threading.Thread(
        target=lambda: active_results.append(coordinator.confirm(active))
    )
    waiting_thread = threading.Thread(
        target=lambda: waiting_results.append(coordinator.review(waiting))
    )
    active_thread.start()
    assert adapter.entered.wait(timeout=1)
    waiting_thread.start()
    deadline = time.monotonic() + 1
    while waiting.request_id not in coordinator.pending_request_ids:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert coordinator.shutdown(reason="application shutdown") == 2
    waiting_thread.join(timeout=1)
    assert waiting_results[0].cancelled is True
    assert waiting_results[0].reason == "application shutdown"
    assert adapter.cancelled == [active.request_id]

    adapter.release.set()
    active_thread.join(timeout=1)
    assert active_results[0].cancelled is True
    assert coordinator.pending_request_ids == ()
    assert coordinator.is_shutdown is True

    rejected = coordinator.review(ReviewRequest(title="late", summary="late"))
    assert rejected.cancelled is True
    assert rejected.reason == "application shutdown"


def test_foreground_prompt_and_background_interaction_share_one_input_slot() -> None:
    adapter = _Adapter()
    adapter.release.set()
    coordinator = InteractionCoordinator(adapter)
    request = ReviewRequest(title="background", summary="background")
    results = []

    with coordinator.foreground_input() as available:
        assert available is True
        thread = threading.Thread(
            target=lambda: results.append(coordinator.review(request))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while request.request_id not in coordinator.pending_request_ids:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert adapter.calls == []
        coordinator.cancel_all(reason="session reset")

    thread.join(timeout=1)
    assert results[0].cancelled is True
    assert results[0].reason == "session reset"
    assert adapter.calls == []
