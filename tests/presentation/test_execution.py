from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    OperationPhaseChanged,
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
    SubagentJobChanged,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
)
from reuleauxcoder.presentation.execution import (
    ExecutionViewReducer,
    execution_panel_lines,
    execution_panel_view,
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
                    {
                        "step": "research",
                        "active_form": "researching",
                        "status": "completed",
                    },
                    {
                        "step": "implement",
                        "active_form": "implementing",
                        "status": "in_progress",
                    },
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
    assert tuple(reducer.state.agents["main"].output_tail) == ("3", "4", "5", "6", "7")
    reducer.apply(
        _event(
            ToolCallFinished(
                "tc1", "shell", ToolOutcome.from_legacy("complete", success=True)
            ),
            event_id="finish",
        )
    )
    assert reducer.state.agents["main"].status == "working"


def test_operation_phase_projects_exact_activity_and_elapsed_time() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(
        _event(
            OperationPhaseChanged(
                operation_id="request-1",
                operation="model",
                phase="await_first_chunk",
                started_at=100.0,
                cancelable=True,
            ),
            event_id="phase",
        )
    )

    view = execution_panel_view(reducer.state, now=103.25)

    assert "waiting for first response" in view.main.activity
    assert "3.2s" in view.main.activity
    assert "Ctrl+C cancels" in view.main.activity


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
    assert "NEED 1" in wide[0]
    assert any("Review file edit" in line for line in wide)
    assert len(narrow) == 3
    assert all(len(line) <= 40 for line in narrow)

    reducer.apply(_event(ApprovalResolved("approval-1", True), event_id="resolved"))
    assert not reducer.state.attention


def test_panel_view_keeps_semantics_separate_from_terminal_layout() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(
        _event(
            ProgressReported(1, "implementing", "building panel", "verify"),
            event_id="progress-view",
        )
    )

    view = execution_panel_view(reducer.state, now=100.1)

    assert view.phase == "IMPLEMENTING"
    assert view.main.label == "MAIN"
    assert view.main.activity == "building panel"
    assert view.progress_next == "verify"


def test_stale_generation_and_duplicate_events_are_ignored() -> None:
    reducer = ExecutionViewReducer()
    current = _event(ProgressReported(2, "verifying", "current", None), event_id="same")
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


def test_panel_animates_only_inside_real_event_lease() -> None:
    reducer = ExecutionViewReducer(animation_lease_seconds=1.0)
    reducer.apply(
        _event(ToolCallStarted("tc", "shell", {"command": "tests"}), event_id="tool")
    )

    first = execution_panel_lines(reducer.state, width=100, now=100.1)
    second = execution_panel_lines(reducer.state, width=100, now=100.4)
    expired = execution_panel_lines(reducer.state, width=100, now=101.1)

    assert first != second
    assert any(marker in "\n".join(first) for marker in ("◐", "◓", "◑", "◒"))
    assert not any(marker in "\n".join(expired) for marker in ("◐", "◓", "◑", "◒"))


def test_panel_expands_plan_only_when_details_are_requested() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(
        _event(
            PlanUpdated(
                revision=1,
                items=(
                    {"step": "done step", "active_form": "done", "status": "completed"},
                    {
                        "step": "active step",
                        "active_form": "working actively",
                        "status": "in_progress",
                    },
                ),
            ),
            event_id="plan-expand",
        )
    )

    expanded = execution_panel_lines(reducer.state, width=100, now=100.5, expanded=True)
    collapsed = execution_panel_lines(reducer.state, width=100, now=100.5)

    assert any("done step" in line for line in expanded)
    assert any("working actively" in line for line in expanded)
    assert not any("done step" in line for line in collapsed)
    assert any("working actively" in line for line in collapsed)
    assert len(collapsed) == 4


def test_subagent_panel_shows_live_activity_budget_and_blocker() -> None:
    reducer = ExecutionViewReducer()
    reducer.apply(
        _event(
            SubagentJobChanged(
                job_id="sj-live",
                mode="explore",
                task="inspect parser",
                status="running",
                activity="reading symbols",
                current_tool="lsp",
                tool_calls=4,
                max_tool_calls=20,
                tokens=1200,
                max_tokens=8000,
            ),
            event_id="child-live",
            agent_id="main",
        )
    )
    lines = execution_panel_lines(reducer.state, width=140, now=100.1)
    rendered = "\n".join(lines)

    assert "inspect parser" in rendered
    assert "running lsp" in rendered
    assert "tools 4/20 · tok 1200/8000" in rendered
    assert "LIVE" in lines[0]


def test_subagent_lifecycle_and_worker_events_share_one_panel_row() -> None:
    reducer = ExecutionViewReducer(root_agent_id="main")
    reducer.apply(
        _event(
            SubagentJobChanged(
                job_id="sj_same",
                mode="explore",
                task="inspect tests",
                status="running",
                child_agent_id="sa_sj_same",
            ),
            event_id="job-running",
        )
    )
    reducer.apply(
        _event(
            ToolCallStarted("child-tool", "grep", {"pattern": "approval"}),
            event_id="child-tool",
            agent_id="sa_sj_same",
        )
    )

    assert list(reducer.state.agents) == ["sj_same"]
    assert reducer.state.agents["sj_same"].activity == "grep approval"


def test_subagent_lifecycle_merges_worker_event_that_arrives_first() -> None:
    reducer = ExecutionViewReducer(root_agent_id="main")
    reducer.apply(
        _event(
            ToolCallStarted("child-tool", "read_file", {"file_path": "a.py"}),
            event_id="child-tool-first",
            agent_id="sa_sj_race",
        )
    )
    reducer.apply(
        _event(
            SubagentJobChanged(
                job_id="sj_race",
                mode="explore",
                task="inspect a.py",
                status="running",
                child_agent_id="sa_sj_race",
            ),
            event_id="job-second",
        )
    )

    assert list(reducer.state.agents) == ["sj_race"]
    assert reducer.state.agents["sj_race"].task == "inspect a.py"


def test_terminal_subagent_remains_until_next_subagent_starts() -> None:
    reducer = ExecutionViewReducer(root_agent_id="main")
    reducer.apply(
        _event(
            SubagentJobChanged(
                job_id="sj_old",
                mode="explore",
                task="old task",
                status="completed",
            ),
            event_id="old-completed",
        )
    )

    assert "sj_old" in reducer.state.agents
    assert any(
        "old task" in line
        for line in execution_panel_lines(reducer.state, width=100, now=101.0)
    )

    reducer.apply(
        _event(
            SubagentJobChanged(
                job_id="sj_new",
                mode="explore",
                task="new task",
                status="queued",
            ),
            event_id="new-queued",
            timestamp=102.0,
        )
    )

    assert "sj_old" not in reducer.state.agents
    assert "sj_new" in reducer.state.agents
