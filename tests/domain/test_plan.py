import pytest

from reuleauxcoder.domain.agent.agent import Agent


class _LLM:
    model = "model"


def _items(status="in_progress"):
    return [
        {
            "step": "Implement history",
            "active_form": "Implementing history",
            "status": status,
        },
        {
            "step": "Verify resume",
            "active_form": "Verifying resume",
            "status": "pending",
        },
    ]


def test_plan_commit_is_ledger_first_and_tool_call_idempotent() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    state, changed = agent.plan_controller.update(
        _items(),
        explanation="Start implementation",
        tool_call_id="call_1",
        session_generation=0,
    )
    same, changed_again = agent.plan_controller.update(
        _items(),
        explanation="Start implementation",
        tool_call_id="call_1",
        session_generation=0,
    )

    assert changed is True
    assert changed_again is False
    assert same.revision == state.revision == 1
    events = [
        event for event in agent.history_ledger.events if event.kind == "plan_updated"
    ]
    assert len(events) == 1
    assert state.event_id == events[0].event_id


def test_invalid_plan_does_not_mutate_state_or_ledger() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    invalid = _items()
    invalid[1]["status"] = "in_progress"

    with pytest.raises(ValueError, match="at most one"):
        agent.plan_controller.update(
            invalid,
            explanation=None,
            tool_call_id="call",
            session_generation=0,
        )

    assert agent.plan_controller.state.revision == 0
    assert agent.history_ledger.events == ()


def test_generation_change_rejects_stale_plan_commit() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.session_generation = 2
    with pytest.raises(ValueError, match="generation changed"):
        agent.plan_controller.update(
            _items(),
            explanation=None,
            tool_call_id="call",
            session_generation=1,
        )


def test_same_tool_call_cannot_commit_different_control_data() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.plan_controller.update(
        _items(), explanation=None, tool_call_id="call", session_generation=0
    )
    with pytest.raises(ValueError, match="different plan data"):
        agent.plan_controller.update(
            [], explanation=None, tool_call_id="call", session_generation=0
        )


def test_progress_updates_do_not_touch_context_compression_policy() -> None:
    agent = Agent(llm=_LLM(), tools=[])

    class CompressionPolicyMustNotBeRead:
        def __getattribute__(self, name):
            raise AssertionError(f"progress must not access context.{name}")

    agent.context = CompressionPolicyMustNotBeRead()

    progress, changed = agent.plan_controller.report(
        phase="ready",
        summary="Implementation and verification are complete.",
        next_step=None,
        tool_call_id="progress_ready",
        session_generation=0,
    )

    assert changed is True
    assert progress.phase == "ready"


def test_snapshot_failure_recovers_control_state_and_idempotency_from_ledger() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    calls = 0

    def persist():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk temporarily unavailable")

    agent._session_persist_callback = persist
    state, changed = agent.plan_controller.update(
        _items(),
        explanation="ledger is authoritative",
        tool_call_id="call_recover",
        session_generation=0,
    )
    assert changed is True and state.revision == 1
    assert agent._control_plane_recovery_required is True
    agent.plan_controller.restore(None, None)

    assert agent.recover_control_plane_if_required() is True
    assert agent.plan_controller.state.revision == 1
    same, changed_again = agent.plan_controller.update(
        _items(),
        explanation="ledger is authoritative",
        tool_call_id="call_recover",
        session_generation=0,
    )
    assert changed_again is False and same.revision == 1
    assert agent._control_plane_recovery_required is False
    assert agent.history_ledger.events[-1].kind == "control_state_recovered"


def test_unrecoverable_control_snapshot_blocks_next_request_boundary() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._session_persist_callback = lambda: (_ for _ in ()).throw(OSError("disk"))
    agent.plan_controller.update(
        _items(), explanation=None, tool_call_id="call", session_generation=0
    )

    assert agent._control_plane_recovery_required is True
    assert agent.recover_control_plane_if_required() is False
