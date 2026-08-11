from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_outcome import ToolDiff, ToolOutcome
from reuleauxcoder.domain.runtime.events import agent_event_to_runtime_event
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    DiagnosticsCleared,
    DiagnosticsPublished,
    RuntimeDiagnostic,
    RuntimeEvent,
    RuntimeStateChanged,
    SessionChanged,
    ToolCallFinished,
    ToolOutputDelta,
    ViewRefreshed,
    ViewRequested,
)
from reuleauxcoder.presentation.models import (
    ApprovalCell,
    AssistantCell,
    DiagnosticCell,
    DiffCell,
    ToolCell,
    ToolCellStatus,
    UserCell,
)
from reuleauxcoder.presentation.reducer import PresentationReducer, RuntimeViewState
from reuleauxcoder.presentation.models import TranscriptModel


def _runtime(event: AgentEvent, *, turn_id: str = "turn-1"):
    return agent_event_to_runtime_event(event, turn_id=turn_id)


def test_stream_deltas_merge_into_one_assistant_cell() -> None:
    reducer = PresentationReducer()

    reducer.apply(_runtime(AgentEvent.stream_token("hello ")))
    reducer.apply(_runtime(AgentEvent.stream_token("world")))
    reducer.apply(_runtime(AgentEvent.chat_end("hello world", render_response=False)))

    assert reducer.state.transcript.cells == (
        AssistantCell(
            id="assistant:turn-1",
            text="hello world",
            complete=True,
            revision=2,
            group_id="agent:session:0:turn-1",
        ),
    )


def test_turn_finished_reconciles_a_partial_stream() -> None:
    reducer = PresentationReducer()

    reducer.apply(_runtime(AgentEvent.stream_token("hel")))
    reducer.apply(
        _runtime(AgentEvent.chat_end("hello world", render_response=False))
    )

    cells = reducer.state.transcript.cells
    assert len(cells) == 1
    assert isinstance(cells[0], AssistantCell)
    assert cells[0].text == "hello world"
    assert cells[0].complete is True


def test_turn_finished_recovers_when_all_stream_deltas_were_dropped() -> None:
    reducer = PresentationReducer()

    reducer.apply(
        _runtime(AgentEvent.chat_end("complete response", render_response=False))
    )

    cells = reducer.state.transcript.cells
    assert len(cells) == 1
    assert isinstance(cells[0], AssistantCell)
    assert cells[0].text == "complete response"
    assert cells[0].complete is True


def test_turn_finished_does_not_duplicate_an_exact_stream() -> None:
    reducer = PresentationReducer()

    reducer.apply(_runtime(AgentEvent.stream_token("complete response")))
    reducer.apply(
        _runtime(AgentEvent.chat_end("complete response", render_response=False))
    )

    cells = reducer.state.transcript.cells
    assert len(cells) == 1
    assert isinstance(cells[0], AssistantCell)
    assert cells[0].text == "complete response"
    assert cells[0].complete is True


def test_turn_finished_recovers_after_interrupted_retry_deltas_were_dropped() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.stream_token("interrupted partial")))
    reducer.apply(
        _runtime(
            AgentEvent.assistant_stream_interrupted(
                attempt_id="turn-1:0:1",
                interrupt_epoch=1,
            )
        )
    )

    reducer.apply(_runtime(AgentEvent.chat_end("retry final", render_response=False)))

    assistants = [
        cell
        for cell in reducer.state.transcript.cells
        if isinstance(cell, AssistantCell)
    ]
    assert [cell.text for cell in assistants] == [
        "interrupted partial",
        "retry final",
    ]
    assert assistants[0].interrupted is True
    assert assistants[1].complete is True
    assert assistants[0].id != assistants[1].id


def test_turn_finished_recovers_after_post_tool_deltas_were_dropped() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.stream_token("before tool")))
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="tool-1"))
    )

    reducer.apply(_runtime(AgentEvent.chat_end("after tool", render_response=False)))

    assistants = [
        cell
        for cell in reducer.state.transcript.cells
        if isinstance(cell, AssistantCell)
    ]
    assert [cell.text for cell in assistants] == ["before tool", "after tool"]
    assert all(cell.complete for cell in assistants)
    assert assistants[0].id != assistants[1].id


def test_empty_turn_finished_response_clears_partial_stream() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.stream_token("stale partial")))

    reducer.apply(_runtime(AgentEvent.chat_end("", render_response=False)))

    cell = reducer.state.transcript.cells[0]
    assert isinstance(cell, AssistantCell)
    assert cell.text == ""
    assert cell.complete is True


def test_interrupted_stream_is_sealed_and_retry_opens_a_new_cell() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.stream_token("partial")))
    reducer.apply(
        _runtime(
            AgentEvent.assistant_stream_interrupted(
                attempt_id="turn-1:0:1",
                interrupt_epoch=1,
            )
        )
    )
    reducer.apply(_runtime(AgentEvent.stream_token("replacement")))

    partial, replacement = reducer.state.transcript.cells
    assert isinstance(partial, AssistantCell)
    assert partial.text == "partial"
    assert partial.complete is True
    assert partial.interrupted is True
    assert isinstance(replacement, AssistantCell)
    assert replacement.text == "replacement"
    assert replacement.interrupted is False


def test_tool_call_splits_pre_and_post_tool_assistant_text_in_visual_order() -> None:
    reducer = PresentationReducer()

    reducer.apply(_runtime(AgentEvent.stream_token("before tool")))
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="tool-1"))
    )
    reducer.apply(
        RuntimeEvent(
            payload=ApprovalRequested(
                request_id="approval-1",
                title="Approval required: shell",
            )
        )
    )
    reducer.apply(
        _runtime(AgentEvent.tool_call_end("shell", "done", tool_call_id="tool-1"))
    )
    reducer.apply(_runtime(AgentEvent.stream_token("after tool")))

    before, tool, approval, after = reducer.state.transcript.cells
    assert isinstance(before, AssistantCell)
    assert before.text == "before tool"
    assert before.complete is True
    assert isinstance(tool, ToolCell)
    assert isinstance(approval, ApprovalCell)
    assert isinstance(after, AssistantCell)
    assert after.text == "after tool"


def test_user_cell_hides_resume_lifecycle_prefix() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        _runtime(
            AgentEvent.chat_start(
                "[SESSION_RESUME] User returned at now.\n\ncontinue the work"
            )
        )
    )
    assert reducer.state.transcript.cells == (
        UserCell(
            id="user:turn-1",
            text="continue the work",
            group_id="agent:session:0:turn-1",
        ),
    )


def test_parallel_tool_ends_update_their_own_cells_out_of_order() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="a")))
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("read_file", {}, tool_call_id="b"))
    )
    reducer.apply(
        _runtime(AgentEvent.tool_call_end("read_file", "B", tool_call_id="b"))
    )
    reducer.apply(_runtime(AgentEvent.tool_call_end("shell", "A", tool_call_id="a")))

    first, second = reducer.state.transcript.cells
    assert isinstance(first, ToolCell) and first.outcome.model_text == "A"
    assert isinstance(second, ToolCell) and second.outcome.model_text == "B"
    assert first.status is ToolCellStatus.SUCCEEDED
    assert second.status is ToolCellStatus.SUCCEEDED


def test_orphan_tool_end_is_explicit_and_deduplicated() -> None:
    reducer = PresentationReducer()
    event = _runtime(AgentEvent.tool_call_end("shell", "ok", tool_call_id="x"))

    reducer.apply(event)
    reducer.apply(event)

    (cell,) = reducer.state.transcript.cells
    assert isinstance(cell, ToolCell)
    assert cell.orphaned is True


def test_structured_tool_diff_becomes_a_correlated_diff_cell() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("edit_file", {}, tool_call_id="x"))
    )
    event = RuntimeEvent(
        payload=ToolCallFinished(
            tool_call_id="x",
            tool_name="edit_file",
            outcome=ToolOutcome(
                summary="Edited main.py",
                diff=ToolDiff(path="main.py", unified="--- a/main.py\n+++ b/main.py\n"),
            ),
        )
    )

    changes = reducer.apply(event)

    assert [change.cell.id for change in changes if change.cell is not None] == [
        "tool:x",
        "diff:x",
    ]
    diff = reducer.state.transcript.get("diff:x")
    assert isinstance(diff, DiffCell)
    assert diff.path == "main.py"


def test_reviewed_tool_diff_is_not_duplicated_as_a_result_cell() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("edit_file", {}, tool_call_id="x"))
    )
    event = RuntimeEvent(
        payload=ToolCallFinished(
            tool_call_id="x",
            tool_name="edit_file",
            outcome=ToolOutcome(
                summary="Edited main.py",
                diff=ToolDiff(path="main.py", unified="--- a/main.py\n+++ b/main.py\n"),
                metadata={"diff_reviewed": True},
            ),
        )
    )

    reducer.apply(event)

    assert reducer.state.transcript.get("diff:x") is None


def test_transcript_retention_is_bounded_and_reindexed() -> None:
    state = RuntimeViewState(transcript=TranscriptModel(max_cells=2))
    reducer = PresentationReducer(state=state)

    for index in range(3):
        reducer.apply(
            _runtime(
                AgentEvent.tool_call_start(
                    "shell", {"i": index}, tool_call_id=str(index)
                )
            )
        )

    assert [cell.id for cell in state.transcript.cells] == ["tool:1", "tool:2"]
    assert state.transcript.get("tool:0") is None
    assert state.transcript.get("tool:1") is state.transcript.cells[0]


def test_same_event_sequence_produces_equal_state() -> None:
    legacy = [
        AgentEvent.stream_token("a"),
        AgentEvent.stream_token("b"),
        AgentEvent.chat_end("ab", render_response=False),
    ]
    events = [_runtime(event) for event in legacy]
    left = PresentationReducer()
    right = PresentationReducer()

    for event in events:
        left.apply(event)
        right.apply(event)

    assert left.state.transcript.cells == right.state.transcript.cells
    assert left.state.seen_event_ids == right.state.seen_event_ids


def test_late_event_from_older_session_generation_is_rejected() -> None:
    reducer = PresentationReducer()
    current = AgentEvent.stream_token("current")
    current.agent_id = "agent-1"
    current.session_generation = 2
    current.session_id = "session-1"
    stale = AgentEvent.stream_token(" stale")
    stale.agent_id = "agent-1"
    stale.session_generation = 1
    stale.session_id = "session-1"

    reducer.apply(_runtime(current))
    changes = reducer.apply(_runtime(stale))

    assert changes == ()
    assert reducer.state.transcript.cells == (
        AssistantCell(
            id="assistant:agent-1:turn-1",
            text="current",
            group_id="agent-1:session-1:2:turn-1",
        ),
    )


def test_generation_watermarks_are_isolated_by_agent_and_session() -> None:
    reducer = PresentationReducer()
    parent = AgentEvent.stream_token("parent")
    parent.agent_id = "parent"
    parent.session_generation = 3
    parent.session_id = "session"
    child = AgentEvent.stream_token("child")
    child.agent_id = "child"
    child.session_generation = 1
    child.session_id = "session"

    assert reducer.apply(_runtime(parent))
    assert reducer.apply(_runtime(child))
    assert reducer.state.transcript.cells == (
        AssistantCell(
            id="assistant:parent:turn-1",
            text="parent",
            group_id="parent:session:3:turn-1",
        ),
        AssistantCell(
            id="assistant:child:turn-1",
            text="child",
            group_id="child:session:1:turn-1",
        ),
    )


def test_tool_output_delta_correlates_with_running_tool() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="x")))

    changes = reducer.apply(
        RuntimeEvent(payload=ToolOutputDelta(tool_call_id="x", text="hello"))
    )

    assert changes
    cell = reducer.state.transcript.get("tool:x")
    assert isinstance(cell, ToolCell)
    assert cell.output == "hello"


def test_tool_output_delta_retains_only_five_ui_lines() -> None:
    reducer = PresentationReducer()
    reducer.apply(_runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="x")))

    reducer.apply(
        RuntimeEvent(
            payload=ToolOutputDelta(
                tool_call_id="x",
                text="\n".join(f"line-{index}" for index in range(10)),
            )
        )
    )

    cell = reducer.state.transcript.get("tool:x")
    assert isinstance(cell, ToolCell)
    assert cell.output == "\n".join(f"line-{index}" for index in range(5, 10))


def test_diagnostic_publish_and_clear_update_same_typed_cell() -> None:
    reducer = PresentationReducer()
    published = RuntimeEvent(
        payload=DiagnosticsPublished(
            batch_id="batch-1",
            file_path="main.py",
            document_version=2,
            diagnostic_generation=4,
            diagnostics=(RuntimeDiagnostic(line=1, character=2, message="broken"),),
        ),
        agent_id="agent-1",
    )
    reducer.apply(published)

    cleared = RuntimeEvent(
        payload=DiagnosticsCleared(
            batch_id="batch-2",
            file_path="main.py",
            document_version=3,
            diagnostic_generation=5,
        ),
        agent_id="agent-1",
    )
    changes = reducer.apply(cleared)

    assert changes
    cell = reducer.state.transcript.get("diagnostic:agent-1:main.py")
    assert isinstance(cell, DiagnosticCell)
    assert cell.batch_id == "batch-2"
    assert cell.document_version == 3
    assert cell.diagnostics == ()


def test_approval_request_and_resolution_correlate_by_request_id() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        RuntimeEvent(
            payload=ApprovalRequested(
                request_id="approval-1", title="Run shell", preview="echo ok"
            )
        )
    )
    reducer.apply(
        RuntimeEvent(
            payload=ApprovalResolved(
                request_id="approval-1",
                approved=True,
                reason="approved",
                mode="allow_session",
                grant_label="This directory",
                released_count=2,
                resolution_source="user",
            )
        )
    )

    cell = reducer.state.transcript.get("approval:approval-1")
    assert isinstance(cell, ApprovalCell)
    assert cell.status == "approved"
    assert cell.reason == "approved"
    assert cell.mode == "allow_session"
    assert cell.grant_label == "This directory"
    assert cell.released_count == 2
    assert cell.resolution_source == "user"


def test_session_runtime_and_view_events_update_typed_view_state() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        RuntimeEvent(payload=SessionChanged(action="restore", session_id="s-1"))
    )
    reducer.apply(RuntimeEvent(payload=RuntimeStateChanged(state="running")))
    reducer.apply(
        RuntimeEvent(payload=ViewRequested(request_id="v-1", view_type="models"))
    )
    reducer.apply(
        RuntimeEvent(
            payload=ViewRefreshed(request_id="v-1", view_type="models", revision=2)
        )
    )

    assert reducer.state.active_session_id == "s-1"
    assert reducer.state.runtime_state == "running"
    assert reducer.state.view_revisions[("v-1", "models")] == 2
