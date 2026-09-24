import threading

from reuleauxcoder.app.interaction_contracts import ConfirmRequest
from reuleauxcoder.app.rpc.codec import decode, encode


def test_frontend_retains_cancel_before_request_worker_registration(runtime):
    request = ConfirmRequest("Question", "Continue?")
    called = []
    runtime.interactor.confirm = lambda request: called.append(request)
    runtime.client._cancel_interaction(request.request_id)
    response = decode(runtime.client._interact("confirm", encode(request)))
    assert response.cancelled
    assert not called
    assert not runtime.client._active_interactions


def test_backend_cancel_between_activation_and_remote_registration(runtime):
    coordinator = runtime.server.interactions
    adapter = coordinator.adapter
    entered, release = threading.Event(), threading.Event()
    original = adapter.confirm
    request = ConfirmRequest("Question", "Continue?")
    responses = []

    def delayed(request):
        entered.set()
        assert release.wait(2)
        return original(request)

    adapter.confirm = delayed
    worker = threading.Thread(target=lambda: responses.append(coordinator.confirm(request)))
    worker.start()
    try:
        assert entered.wait(1)
        assert coordinator.cancel(request.request_id)
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert responses[0].cancelled
        assert not coordinator.pending_request_ids
    finally:
        release.set()
        worker.join(2)


def test_backend_cancel_finishes_without_waiting_for_frontend_response(runtime):
    entered, release = threading.Event(), threading.Event()
    request = ConfirmRequest("Question", "Continue?")
    responses = []
    runtime.client.peer.methods["interaction.request"] = lambda **_: (
        entered.set(), release.wait(3), None
    )[-1]
    coordinator = runtime.server.interactions
    worker = threading.Thread(target=lambda: responses.append(coordinator.confirm(request)))
    worker.start()
    try:
        assert entered.wait(1)
        coordinator.cancel(request.request_id)
        worker.join(1)
        assert not worker.is_alive()
        assert responses[0].cancelled
        assert not coordinator.pending_request_ids
        assert not runtime.server.peer._pending
    finally:
        release.set()
        worker.join(2)
