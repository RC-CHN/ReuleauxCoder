import pytest

from reuleauxcoder.domain.agent.agent import Agent


class _LLM:
    model = "model"


def _items(status="in_progress"):
    return [
        {"step": "Implement history", "active_form": "Implementing history", "status": status},
        {"step": "Verify resume", "active_form": "Verifying resume", "status": "pending"},
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
    events = [event for event in agent.history_ledger.events if event.kind == "plan_updated"]
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
