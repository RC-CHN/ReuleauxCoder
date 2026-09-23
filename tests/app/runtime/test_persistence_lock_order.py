"""Deterministic interleavings for snapshot/control lock ordering.

Run in a subprocess so a regression cannot leave deadlocked pytest workers.
"""

import multiprocessing
import threading

import pytest

from reuleauxcoder.app.runtime.session_state import (
    _LiveSessionPersistence,
    build_session_runtime_state,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config


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
    process = multiprocessing.get_context("spawn").Process(
        target=_control_during_snapshot,
        args=(operation,),
        daemon=True,
    )
    process.start()
    try:
        process.join(8)
        assert not process.is_alive(), "Control tool and snapshot writer deadlocked"
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(3)
