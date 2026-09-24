"""Relay views use the same real RPC runtime as terminal and desktop clients."""

import threading
import time

import pytest

from reuleauxcoder.app.interaction_contracts import ConfirmRequest
from reuleauxcoder.app.runtime.session_state import save_session_snapshot
from reuleauxcoder.extensions.remote_exec.http_service import _RemoteChatSession
from reuleauxcoder.extensions.remote_exec.protocol import TerminalCapabilities
from reuleauxcoder.infrastructure.rpc.peer import RpcError
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.relay import RelayUI


@pytest.fixture
def relay_view(runtime):
    view = RelayUI(TerminalCapabilities)
    view.bus = runtime.bus
    view.connect(runtime.client)
    runtime.client.interactor = view
    runtime.server.peer.methods["runtime.checkpoint"] = lambda: None
    try:
        yield view
    finally:
        view.close()


def test_relay_interaction_cancellation_and_later_chat_keep_runtime(
    runtime, relay_view
):
    session = _RemoteChatSession("chat", "peer")
    results = []

    def run():
        answer = runtime.agent.ui_interactor.confirm(
            ConfirmRequest(title="Confirm", message="Continue?")
        )
        return "cancelled" if answer.cancelled else "approved"

    runtime.loop.run = run
    worker = threading.Thread(
        target=lambda: results.append(relay_view.run("hello", session))
    )
    worker.start()
    try:
        with session.cond:
            assert session.cond.wait_for(
                lambda: any(
                    event["type"] == "interaction_request" for event in session.events
                ),
                timeout=3,
            )
            request = next(
                event
                for event in session.events
                if event["type"] == "interaction_request"
            )
        relay_view.cancel(request["payload"]["request_id"])
        worker.join(3)
        assert not worker.is_alive()
        assert results[0].response == "cancelled"
        assert not relay_view._interaction_sessions
        assert relay_view.session is None
        assert not runtime.client.peer.closed.is_set()
        runtime.loop.run = lambda: "second chat"
        assert relay_view.run("again").response == "second chat"
    finally:
        session.cancel_pending_approvals("test finished")
        worker.join(3)


def test_relay_clears_stream_binding_when_checkpoint_fails(runtime, relay_view):
    def fail():
        raise RuntimeError("save failed")

    runtime.server.peer.methods["runtime.checkpoint"] = fail
    with pytest.raises(RpcError):
        relay_view.run("hello", _RemoteChatSession("chat", "peer"))
    assert relay_view.session is None
    assert not runtime.client.state.running


def test_relay_honors_cancellation_before_interaction_registration(runtime, relay_view):
    session = _RemoteChatSession("chat", "peer")
    relay_view.session = session
    request = ConfirmRequest(
        title="Confirm", message="Continue?", deadline=time.monotonic()
    )
    response = relay_view.confirm(request)
    assert response.cancelled and not response.confirmed
    assert not session.interaction_waiters
    assert not relay_view._interaction_sessions


@pytest.mark.parametrize("first", ["checkpoint", "shutdown"])
def test_checkpoint_and_shutdown_serialize_session_writes(runtime, monkeypatch, first):
    runtime.config.session_auto_save = True
    runtime.client.submit("preserve work")
    runtime.client.wait_idle()
    commands = runtime.server.commands
    release = threading.Event()
    entered = {kind: threading.Event() for kind in ("checkpoint", "shutdown")}
    dispatched = {kind: threading.Event() for kind in entered}
    saves, failures = [], []
    original_save = SessionStore.save

    def save(store, *args, **kwargs):
        kind = "shutdown" if any(
            "[SESSION_EXIT]" in str(message.get("content")) for message in args[0]
        ) else "checkpoint"
        entered[kind].set()
        if kind == first:
            assert release.wait(5)
        result = original_save(store, *args, **kwargs)
        saves.append(kind)
        return result

    monkeypatch.setattr(SessionStore, "save", save)

    def checkpoint():
        dispatched["checkpoint"].set()
        return commands.checkpoint(
            lambda: save_session_snapshot(
                runtime.config, runtime.agent, SessionStore(commands.sessions_dir),
                commands.session_id,
            )
        )

    def shutdown():
        dispatched["shutdown"].set()
        return runtime.server.shutdown()

    runtime.server.peer.methods.update(
        {"runtime.checkpoint": checkpoint, "runtime.shutdown": shutdown}
    )

    def request(kind):
        try:
            runtime.client.peer.request(f"runtime.{kind}")
        except BaseException as error:
            failures.append(error)

    second = "shutdown" if first == "checkpoint" else "checkpoint"
    workers = [
        threading.Thread(target=request, args=(kind,)) for kind in (first, second)
    ]
    workers[0].start()
    try:
        assert entered[first].wait(5)
        workers[1].start()
        assert dispatched[second].wait(5)
        assert not entered[second].wait(0.1)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(5)
    assert not any(worker.is_alive() for worker in workers)
    assert not failures
    assert saves == (
        ["checkpoint", "shutdown"] if first == "checkpoint" else ["shutdown"]
    )
    saved = SessionStore(commands.sessions_dir).load(commands.session_id)
    assert saved is not None
    assert any(
        "[SESSION_EXIT]" in message.get("content", "") for message in saved.messages
    )


def test_relay_waits_for_completion_notification_after_an_idle_snapshot(
    runtime, relay_view
):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    results = []

    def completed(result):
        entered.set()
        assert release.wait(3)
        relay_view._completed(result)

    runtime.client.on_completed = completed

    def run():
        results.append(relay_view.run("hello"))
        finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        # Request responses can overtake the independent notification worker.
        runtime.client.refresh()
        assert not runtime.client.state.running
        assert not finished.wait(0.1)
    finally:
        release.set()
        worker.join(3)
    assert finished.is_set()
    assert results[0].response == "done"
