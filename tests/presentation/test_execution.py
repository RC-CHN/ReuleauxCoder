from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
)
from reuleauxcoder.presentation.execution import (
    ExecutionViewReducer,
    execution_panel_lines,
)


def _event(payload, *, event_id: str, timestamp: float = 100.0, agent_id="main"):
    return RuntimeEvent(
        payload=payload,
        event_id=event_id,
        timestamp=timestamp,
        agent_id=agent_id,
        session_id="s1",
        session_generation=1,
    )


def test_execution_view_reduces_plan_progress_and_real_activity() -> None:
    reducer = ExecutionViewReducer(animation_lease_seconds=1.0)
    reducer.apply(
        _event(
            PlanUpdated(
                revision=2,
                items=(
                    {"step": "research", "active_form": "researching", "status": "completed"},
                    {"step": "implement", "active_form": "implementing", "status": "in_progress"},
                ),
            ),
            event_id="plan",
        )
    )
    reducer.apply(
        _event(
            ProgressReported(3, "implementing", "separating the UI", "verify"),
            event_id="progress",
        )
    )
    reducer.apply(
        _event(
            ToolCallStarted("tc1", "shell", {"command": "pytest"}),
            event_id="tool",
        )
    )

    state = reducer.state
    assert state.plan_revision == 2
    assert state.completed_plan_items == 1
    assert state.phase == "implementing"
    assert state.agents["main"].activity == "shell pytest"
    assert state.agents["main"].is_animating(100.5)
    assert not state.agents["main"].is_animating(101.1)
    assert state.agents["main"].status == "tool"


def test_execution_view_keeps_five_latest_tool_lines_for_humans() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(_event(ToolCallStarted("tc1", "shell", {}), event_id="start"))
    reducer.apply(
        _event(
            ToolOutputDelta("tc1", "1\n2\n3\n4\n5\n6\n7\n"),
            event_id="output",
        )
    )
    assert tuple(reducer.state.agents["main"].output_tail) == (
        "3", "4", "5", "6", "7"
    )
    reducer.apply(
        _event(
            ToolCallFinished(
                "tc1", "shell", ToolOutcome.from_legacy("complete", success=True)
            ),
            event_id="finish",
        )
    )
    assert reducer.state.agents["main"].status == "working"


def test_attention_and_width_aware_projection() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(
        _event(
            ApprovalRequested("approval-1", "Review file edit", "diff"),
            event_id="ask",
        )
    )
    wide = execution_panel_lines(reducer.state, width=100, now=100.1)
    narrow = execution_panel_lines(reducer.state, width=40, now=100.1)
    assert "NEEDS YOU" in wide[0]
    assert any("Review file edit" in line for line in wide)
    assert len(narrow) == 3
    assert all(len(line) <= 40 for line in narrow)

    reducer.apply(_event(ApprovalResolved("approval-1", True), event_id="resolved"))
    assert not reducer.state.attention


def test_stale_generation_and_duplicate_events_are_ignored() -> None:
    reducer = ExecutionViewReducer()
    current = _event(
        ProgressReported(2, "verifying", "current", None), event_id="same"
    )
    assert reducer.apply(current)
    assert not reducer.apply(current)
    stale = RuntimeEvent(
        payload=ProgressReported(3, "ready", "stale", None),
        event_id="stale",
        agent_id="main",
        session_id="s1",
        session_generation=0,
    )
    assert not reducer.apply(stale)
    assert reducer.state.progress_summary == "current"
