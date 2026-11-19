import threading

import pytest

from reuleauxcoder.app.rpc.codec import decode
from reuleauxcoder.domain.images import ChatInput
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
            # Pause is observable before the current turn has finished.
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


@pytest.mark.parametrize("arguments", [None, '{"status":"complete"}', '{"status":'])
def test_truncated_stream_blocks_goal_and_keeps_only_partial_text(runtime, monkeypatch, arguments):
    from types import SimpleNamespace

    from reuleauxcoder.domain.agent.loop import AgentLoop
    from reuleauxcoder.extensions.tools.builtin.goal import UpdateGoalTool
    from reuleauxcoder.services.llm.client import LLM
    from tests.services.test_llm_client import _FakeChunk, _FakeUsage

    calls = []

    def respond(params):
        calls.append(params)
        tools = [] if arguments is None else [SimpleNamespace(
            index=0, id="partial-call",
            function=SimpleNamespace(name="update_goal", arguments=arguments),
        )]
        chunk = _FakeChunk(content="First half", tool_calls=tools, usage=_FakeUsage(100, 10, 80))
        chunk.choices[0].finish_reason = "length"
        return iter([chunk])

    llm = LLM(model="test", api_key="test")
    monkeypatch.setattr(llm, "_call_with_retry", respond)
    runtime.agent.llm = llm
    tool = UpdateGoalTool()
    tool.bind_agent(runtime.agent)
    runtime.agent.tools = [tool]
    runtime.agent._loop = AgentLoop(runtime.agent, prompt_fn=lambda *a, **kw: "Work", shell_name="bash")

    runtime.client.submit("/goal create Finish the task")
    with pytest.raises(RpcError, match="reached its output or context limit"):
        runtime.client.wait_idle()
    assert len(calls) == 1
    assert runtime.client.state.goal.status == "blocked"
    assert runtime.client.state.goal.tokens_used == 30
    partial = runtime.agent.messages[-2]
    assert partial["role"] == "assistant" and partial["content"] == "First half"
    assert "tool_calls" not in partial
    assert "length" in runtime.agent.messages[-1]["content"]
    events = runtime.agent.history_ledger.events
    assert not any(event.kind == "tool_call_started" for event in events)
    lifecycle = [event.payload for event in events if event.kind == "agent_lifecycle"]
    assert [event["state"] for event in lifecycle] == ["turn_started", "turn_failed"]
    assert lifecycle[-1]["finish_reason"] == "length"


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


def test_goal_stays_running_while_a_control_request_finishes(runtime, monkeypatch):
    work_started, release_work = threading.Event(), threading.Event()
    control_started, release_control = threading.Event(), threading.Event()
    boundary = threading.Event()
    commands = runtime.server.commands
    submit, next_pending = commands.submit, commands.next_pending
    turns = []

    def show_goal(value, **kwargs):
        if getattr(value, "action_id", None) == "goal.show":
            control_started.set()
            assert release_control.wait(5)
        return submit(value, **kwargs)

    def check_queue():
        if release_work.is_set():
            boundary.set()
        return next_pending()

    def run():
        turns.append(runtime.agent.messages[-1]["content"])
        if len(turns) == 1:
            work_started.set()
            assert release_work.wait(5)
        else:
            runtime.agent.goal_controller.update(status="complete")
        return "checkpoint"

    monkeypatch.setattr(commands, "submit", show_goal)
    monkeypatch.setattr(commands, "next_pending", check_queue)
    runtime.loop.run = run
    runtime.client.submit("/goal create Work")
    try:
        assert work_started.wait(5)
        runtime.client.submit("/goal")
        assert control_started.wait(5)
        release_work.set()
        assert boundary.wait(5)
        runtime.client.refresh()
        assert runtime.client.state.running
        assert runtime.client.submit("New priority").status == "queued"
    finally:
        release_work.set()
        release_control.set()
    runtime.client.wait_idle()
    assert turns[1:] == ["New priority"]
    assert runtime.client.state.goal.status == "complete"


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
    assert [value.text if isinstance(value, ChatInput) else value for value in calls] == ["", "Prioritize remote support"]


@pytest.mark.parametrize("entry", ["chat", "_chat_turn"], ids=["before-admission", "after-admission"])
def test_input_arriving_at_goal_turn_start_reaches_first_model_request(runtime, monkeypatch, entry):
    from reuleauxcoder.domain.agent.loop import AgentLoop
    from reuleauxcoder.services.llm.client import LLM
    from tests.services.test_llm_client import _FakeChunk

    entering, release = threading.Event(), threading.Event()
    chat = getattr(runtime.agent, entry)
    guidance = "Build only the second example"
    requests = []

    def enter_chat(text, **kwargs):
        if kwargs.get("goal_continuation"):
            entering.set()
            assert release.wait(5)
        return chat(text, **kwargs)

    def respond(params):
        requests.append(params["messages"])
        runtime.agent.goal_controller.update(status="complete")
        return iter([_FakeChunk(content="Finished")])

    llm = LLM(model="test", api_key="test")
    monkeypatch.setattr(llm, "_call_with_retry", respond)
    monkeypatch.setattr(runtime.agent, entry, enter_chat)
    runtime.agent.llm = llm
    runtime.agent._loop = AgentLoop(
        runtime.agent, prompt_fn=lambda shell, **kwargs: "Work", shell_name="bash"
    )
    runtime.client.submit("/goal create Build examples")
    try:
        assert entering.wait(5)
        assert runtime.client.submit(guidance).status in {"queued", "steering"}
    finally:
        release.set()
    runtime.client.wait_idle()
    assert len(requests) == 1
    assert any(guidance in msg["content"] for msg in requests[0] if msg["role"] == "user")


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


def test_model_tool_pipeline_continues_across_round_limits_and_text_replies(
    runtime, monkeypatch
):
    import json
    from copy import deepcopy
    from itertools import groupby
    from types import SimpleNamespace

    from reuleauxcoder.domain.agent.loop import AgentLoop
    from reuleauxcoder.extensions.tools.builtin.goal import (
        CreateGoalTool,
        GetGoalTool,
        UpdateGoalTool,
    )
    from reuleauxcoder.services.llm.client import LLM
    from tests.services.test_llm_client import _FakeChunk, _FakeUsage

    def tool_chunk(name, arguments, call_id):
        return _FakeChunk(
            usage=_FakeUsage(100, 10, 80),
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=call_id,
                    function=SimpleNamespace(
                        name=name, arguments=json.dumps(arguments)
                    ),
                )
            ],
        )

    lookup_ids = [f"check-{number}" for number in range(5)]
    responses = iter(
        [
            tool_chunk("create_goal", {"objective": "Verify progress"}, "create"),
            *(tool_chunk("get_goal", {}, call_id) for call_id in lookup_ids),
            _FakeChunk(content="First checkpoint", usage=_FakeUsage(100, 10, 80)),
            _FakeChunk(content="Second checkpoint", usage=_FakeUsage(100, 10, 80)),
            tool_chunk("update_goal", {"status": "complete"}, "complete"),
            _FakeChunk(content="Verified and finished", usage=_FakeUsage(100, 10, 80)),
        ]
    )
    turns = []
    requests = []

    def respond(params):
        assert params.get("tools"), "An active goal should resume with tools enabled"
        if requests:
            assert params["tools"] == requests[0]["tools"]
        requests.append(deepcopy(params))
        turns.append(runtime.agent._current_turn_id)
        return iter([next(responses)])

    llm = LLM(model="test", api_key="test")
    monkeypatch.setattr(llm, "_call_with_retry", respond)
    runtime.agent.llm = llm
    runtime.agent.max_rounds = 2
    runtime.agent.tools = [CreateGoalTool(), GetGoalTool(), UpdateGoalTool()]
    for tool in runtime.agent.tools:
        tool.bind_agent(runtime.agent)
    runtime.agent._loop = AgentLoop(
        runtime.agent,
        prompt_fn=lambda shell, **kwargs: "Complete the user's request",
        shell_name="bash",
    )
    prompt = "Create a goal and verify progress"
    runtime.client.submit(prompt)
    runtime.client.wait_idle()
    # Three consecutive working-round limits, two text-only turns, then completion.
    assert [len(list(group)) for _, group in groupby(turns)] == [2, 2, 2, 1, 1, 2]
    assert len(set(turns)) == 6
    previous_messages = requests[1]["messages"]
    prefix_length = next(
        i for i, msg in enumerate(previous_messages, 1)
        if msg.get("tool_call_id") == "create"
    )
    prefix = previous_messages[:prefix_length]
    assert {"role": "user", "content": prompt} in prefix
    assert all(request["messages"][:prefix_length] == prefix for request in requests[2:])
    expected_calls = ["create", *lookup_ids, "complete"]
    events = runtime.agent.history_ledger.events
    assert [
        event.payload["tool_call_id"] for event in events
        if event.kind == "tool_call_started"
    ] == expected_calls
    assert [
        msg["tool_call_id"] for msg in requests[-1]["messages"] if msg["role"] == "tool"
    ] == expected_calls
    assert not any(
        event.payload.get("source") == "max_round_summary_instruction"
        for event in events
    )
    goal = runtime.client.state.goal
    assert goal.status == "complete"
    assert goal.token_budget is None
    # Goal creation precedes accounting; each of the nine subsequent requests costs 30.
    assert goal.tokens_used == 270
    assert runtime.agent.messages[-1]["content"] == "Verified and finished"
    assert (
        sum(
            message["role"] == "user" and not is_synthetic_context_message(message)
            for message in runtime.agent.messages
        )
        == 1
    )
