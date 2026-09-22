import threading

import pytest

from reuleauxcoder.app.rpc.codec import decode
from reuleauxcoder.domain.llm.context_messages import is_synthetic_context_message
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def test_goal_continues_after_final_then_stops_when_complete(runtime):
    turns = []

    def run():
        turns.append(runtime.agent._current_turn_id)
        if len(turns) == 3:
            runtime.agent.goal_controller.update(status="complete")
        return "Checkpoint"

    runtime.loop.run = run
    runtime.client.submit("/goal create Complete three checkpoints")
    runtime.client.wait_idle()
    assert len(set(turns)) == 3
    assert runtime.client.state.goal.status == "complete"
    assert all(
        is_synthetic_context_message(message) for message in runtime.agent.messages
    )
    assert (
        decode(runtime.client.peer.request("goal.get")).objective
        == "Complete three checkpoints"
    )


@pytest.mark.parametrize("operation", ["/goal pause", "interrupt"])
def test_pause_preserves_current_execution_but_interrupt_cancels(runtime, operation):
    entered, release = threading.Event(), threading.Event()
    turns = []

    def run():
        turns.append(1)
        entered.set()
        assert release.wait(3)
        return "checkpoint"

    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    assert entered.wait(3)
    try:
        if operation == "interrupt":
            runtime.client.interrupt()
            assert runtime.agent.stop_requested()
        else:
            runtime.client.submit(operation)
            # Wait on the real state notification, not a guessed sleep.
            with runtime.client._condition:
                assert runtime.client._condition.wait_for(
                    lambda: runtime.client.state.goal.status == "paused", 3
                )
            assert not runtime.agent.stop_requested()
    finally:
        release.set()
    runtime.client.wait_idle()
    assert turns == [1]
    assert runtime.client.state.goal.status == "paused"


def test_pending_session_reset_takes_priority_over_goal_continuation(runtime):
    entered, release = threading.Event(), threading.Event()
    calls = []
    runtime.loop.run = lambda: (
        calls.append(1),
        entered.set(),
        release.wait(3),
        "done",
    )[-1]
    runtime.client.submit("/goal create Work")
    assert entered.wait(3)
    try:
        assert runtime.client.submit("/reset").status == "queued"
    finally:
        release.set()
    runtime.client.wait_idle()
    assert calls == [1]
    assert runtime.client.state.goal is None
    runtime.agent.goal_controller.restore(None, runtime.agent.history_ledger.events)
    assert runtime.agent.goal_controller.state is None


def test_error_blocks_goal_without_retrying_whole_turn(runtime):
    calls = []

    def run():
        calls.append(1)
        raise RuntimeError("upstream failed")

    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    with pytest.raises(RpcError, match="upstream failed"):
        runtime.client.wait_idle()
    assert calls == [1]
    assert runtime.client.state.goal.status == "blocked"


def test_budget_change_does_not_resume_and_resume_retains_cumulative_usage(runtime):
    def run():
        control = runtime.agent.goal_controller
        goal = control.state
        control.record_usage(
            goal.id,
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "estimated": False,
            },
        )
        if control.state.tokens_used == 60:
            control.update(status="complete")
        return "done"

    runtime.config.goal_default_token_budget = 30
    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    runtime.client.wait_idle()
    assert runtime.client.state.goal.status == "budget_limited"
    runtime.client.submit("/goal budget 100")
    runtime.client.wait_idle()
    assert runtime.client.state.goal.status == "budget_limited"
    runtime.client.submit("/goal resume")
    runtime.client.wait_idle()
    assert runtime.client.state.goal.tokens_used == 60
    assert runtime.client.state.goal.status == "complete"


@pytest.mark.parametrize("runtime", [False], indirect=True)
def test_restored_active_goal_waits_for_frontend_ready(runtime):
    runtime.agent.goal_controller.create("Restored work", None)
    called = []

    def run():
        called.append(1)
        runtime.agent.goal_controller.update(status="complete")
        return "done"

    runtime.loop.run = run
    assert not runtime.server._ready
    assert not runtime.client.state.running
    runtime.client.ready()
    runtime.client.wait_idle()
    assert called == [1]


def test_planner_does_not_automatically_continue(runtime):
    runtime.agent.active_mode = "planner"
    runtime.client.submit("/goal create Plan migration")
    runtime.client.wait_idle()
    assert runtime.client.state.goal.status == "active"
    assert runtime.agent.messages == []


def test_interrupt_cancels_request_before_waiting_for_goal_snapshot(runtime):
    entered = threading.Event()

    def persist():
        with runtime.agent._context_revision_lock:
            pass

    def run():
        # Manual compaction holds the context lock across its LLM request.
        with runtime.agent._context_revision_lock:
            entered.set()
            assert runtime.agent._stop_event.wait(3)
        return "cancelled"

    runtime.agent._session_persist_callback = persist
    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    assert entered.wait(3)
    runtime.client.interrupt()
    runtime.client.wait_idle()
    assert runtime.client.state.goal.status == "paused"


def test_shutdown_preserves_active_goal_for_session_resume(runtime):
    entered = threading.Event()
    runtime.loop.run = lambda: (
        entered.set(),
        runtime.agent._stop_event.wait(3),
        "closed",
    )[-1]
    runtime.client.submit("/goal create Work after reconnect")
    assert entered.wait(3)
    runtime.client.shutdown()
    assert runtime.agent.goal_controller.state.status == "active"
    assert not runtime.server._workers


def test_user_input_between_goal_turns_is_queued_before_continuation(
    runtime, monkeypatch
):
    boundary, release = threading.Event(), threading.Event()
    original_chat = runtime.agent.chat
    calls = []

    def chat(text, **kwargs):
        calls.append(text)
        result = original_chat(text, **kwargs)
        if len(calls) == 1:
            boundary.set()
            assert release.wait(3)
        return result

    def run():
        if runtime.agent.messages[-1]["content"] == "Prioritize remote support":
            runtime.agent.goal_controller.update(status="complete")
        return "checkpoint"

    monkeypatch.setattr(runtime.agent, "chat", chat)
    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    assert boundary.wait(3)
    try:
        assert runtime.client.submit("Prioritize remote support").status == "queued"
        assert runtime.client.state.queued_steering == ("Prioritize remote support",)
        assert runtime.client.state.queued_commands == ()
    finally:
        release.set()
    runtime.client.wait_idle()
    assert calls == ["", "Prioritize remote support"]


def test_resume_queued_during_interrupt_starts_after_current_turn_stops(runtime):
    entered, release = threading.Event(), threading.Event()
    turns = []

    def run():
        turns.append(1)
        if len(turns) == 1:
            entered.set()
            assert release.wait(3)
        else:
            assert not runtime.agent.stop_requested()
            runtime.agent.goal_controller.update(status="complete")
        return "checkpoint"

    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    assert entered.wait(3)
    try:
        runtime.client.interrupt()
        assert runtime.client.submit("/goal resume").status == "queued"
    finally:
        release.set()
    runtime.client.wait_idle()
    assert len(turns) == 2
    assert runtime.client.state.goal.status == "complete"


def test_creating_goal_after_interrupted_chat_clears_previous_stop(runtime):
    runtime.agent.request_stop()

    def run():
        runtime.agent.goal_controller.update(status="complete")
        return "done"

    runtime.loop.run = run
    runtime.client.submit("/goal create New work")
    runtime.client.wait_idle()
    assert runtime.client.state.goal.status == "complete"


def test_model_tool_pipeline_creates_continues_and_completes_goal(runtime, monkeypatch):
    import json
    from types import SimpleNamespace

    from reuleauxcoder.domain.agent.loop import AgentLoop
    from reuleauxcoder.extensions.tools.builtin.goal import (
        CreateGoalTool,
        UpdateGoalTool,
    )
    from reuleauxcoder.services.llm.client import LLM
    from tests.services.test_llm_client import _FakeChunk, _FakeUsage

    def tool_chunk(name, arguments):
        return _FakeChunk(
            usage=_FakeUsage(100, 10, 80),
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=name,
                    function=SimpleNamespace(
                        name=name, arguments=json.dumps(arguments)
                    ),
                )
            ],
        )

    responses = iter(
        [
            tool_chunk("create_goal", {"objective": "Verify two checkpoints"}),
            _FakeChunk(content="First checkpoint", usage=_FakeUsage(100, 10, 80)),
            tool_chunk("update_goal", {"status": "complete"}),
            _FakeChunk(content="Verified and finished", usage=_FakeUsage(100, 10, 80)),
        ]
    )
    llm = LLM(model="test", api_key="test")
    monkeypatch.setattr(llm, "_call_with_retry", lambda params: iter([next(responses)]))
    runtime.agent.llm = llm
    runtime.agent.tools = [CreateGoalTool(), UpdateGoalTool()]
    for tool in runtime.agent.tools:
        tool.bind_agent(runtime.agent)
    runtime.agent._loop = AgentLoop(
        runtime.agent,
        prompt_fn=lambda shell, **kwargs: "Complete the user's request",
        shell_name="bash",
    )
    runtime.client.submit("Create a goal and verify two checkpoints")
    runtime.client.wait_idle()
    goal = runtime.client.state.goal
    assert goal.status == "complete"
    assert goal.token_budget is None
    # Creation itself precedes the goal; the checkpoint, completion and final reply count.
    assert goal.tokens_used == 90
    assert runtime.agent.messages[-1]["content"] == "Verified and finished"
    assert (
        sum(
            message["role"] == "user" and not is_synthetic_context_message(message)
            for message in runtime.agent.messages
        )
        == 1
    )
