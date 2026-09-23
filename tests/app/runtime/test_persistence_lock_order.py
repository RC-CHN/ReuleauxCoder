"""Deterministic interleavings for snapshot/control lock ordering.

Run in a subprocess so a regression cannot leave deadlocked pytest workers.
"""

import multiprocessing
import threading
from pathlib import Path

import pytest

from reuleauxcoder.app.runtime.session_state import (
    _LiveSessionPersistence,
    bind_session_persistence,
    build_session_runtime_state,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


class _LLM:
    model = "model"


def _control_during_snapshot(operation):
    agent = Agent(_LLM(), tools=[])
    config = Config(api_key="test")
    writing, publishing = threading.Event(), threading.Event()
    background = None

    def persist():
        if threading.current_thread() is background:
            writing.set()
            assert publishing.wait(3)
        build_session_runtime_state(config, agent)

    persistence = _LiveSessionPersistence(persist)

    def publish():
        publishing.set()
        persistence.flush()

    agent._session_persist_callback = publish
    background = threading.Thread(target=persistence.flush, daemon=True)
    background.start()
    assert writing.wait(3)
    if operation == "plan":
        agent.plan_controller.update(
            [{"step": "Check locks", "status": "in_progress"}],
            explanation=None,
            tool_call_id="plan",
            session_generation=0,
        )
    else:
        agent.plan_controller.report(
            phase="verifying",
            summary="Check locks",
            next_step=None,
            tool_call_id="progress",
            session_generation=0,
        )
    background.join(3)
    assert not background.is_alive()
    assert not agent._control_plane_recovery_required
    assert agent.history_ledger.events[-1].kind in {"plan_updated", "progress_reported"}


@pytest.mark.parametrize("operation", ["plan", "progress"])
def test_control_publish_does_not_hold_state_lock_while_waiting_for_snapshot(operation):
    _run_bounded(_control_during_snapshot, operation)


def _run_bounded(target, *args):
    process = multiprocessing.get_context("spawn").Process(
        target=target,
        args=args,
        daemon=True,
    )
    process.start()
    try:
        process.join(8)
        assert not process.is_alive(), "Concurrent runtime operations deadlocked"
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(3)


class _ObservedLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == "snapshot-contender":
            self.attempted.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


def _context_during_snapshot(directory, operation):
    config = Config(api_key="test", session_dir=directory)
    agent = Agent(_LLM(), tools=[], config=config)
    context_lock = _ObservedLock()
    agent._context_revision_lock = context_lock
    store = SessionStore(Path(directory))
    session_id = store.generate_session_id()
    assert (
        bind_session_persistence(config, agent, store, session_id, fingerprint="local")
        is None
    )
    errors = []

    def flush():
        try:
            agent.persist_runtime_snapshot()
        except BaseException as error:
            errors.append(error)

    with context_lock:
        background = threading.Thread(
            target=flush, name="snapshot-contender", daemon=True
        )
        background.start()
        assert context_lock.attempted.wait(3)
        if operation == "replace":
            agent._replace_context_messages(
                [{"role": "user", "content": "replacement"}], reason="test"
            )
        elif operation == "first_message":
            agent._append_message({"role": "user", "content": "first"}, source="test")
        else:
            agent.goal_controller.create("Verify context-held goal persistence", None)
    background.join(3)
    assert not background.is_alive()
    assert not errors
    agent.unbind_session_persistence()
    loaded = store.load(session_id)
    assert loaded is not None
    if operation == "goal":
        assert (
            loaded.runtime_state.goal["objective"]
            == "Verify context-held goal persistence"
        )
    else:
        assert loaded.messages[0]["content"] == (
            "first" if operation == "first_message" else "replacement"
        )


@pytest.mark.parametrize("operation", ["replace", "first_message", "goal"])
def test_context_mutations_and_snapshot_writer_share_lock_order(tmp_path, operation):
    _run_bounded(_context_during_snapshot, str(tmp_path), operation)


def _steering_during_context_read():
    agent = Agent(_LLM(), tools=[])
    context_lock = _ObservedLock()
    agent._context_revision_lock = context_lock
    agent._accepting_user_steering = True
    agent._current_turn_id = "turn"
    assert agent.admit_user_steering("New direction")
    with context_lock:
        background = threading.Thread(
            target=agent._drain_user_steering, name="snapshot-contender", daemon=True
        )
        background.start()
        assert context_lock.attempted.wait(3)
        # Compression checks this cancellation epoch while holding context.
        assert agent.round_interrupt_epoch() == 0
    background.join(3)
    assert not background.is_alive()
    assert agent.messages[-1]["content"] == "New direction"


def test_steering_drain_does_not_invert_compression_cancellation_locks():
    _run_bounded(_steering_during_context_read)
