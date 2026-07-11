from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.runtime.events import agent_event_to_runtime_event
from reuleauxcoder.presentation.models import AssistantCell, ToolCell, ToolCellStatus
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
        AssistantCell(id="assistant:turn-1", text="hello world", complete=True, revision=2),
    )


def test_parallel_tool_ends_update_their_own_cells_out_of_order() -> None:
    reducer = PresentationReducer()
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("shell", {}, tool_call_id="a"))
    )
    reducer.apply(
        _runtime(AgentEvent.tool_call_start("read_file", {}, tool_call_id="b"))
    )
    reducer.apply(
        _runtime(AgentEvent.tool_call_end("read_file", "B", tool_call_id="b"))
    )
    reducer.apply(
        _runtime(AgentEvent.tool_call_end("shell", "A", tool_call_id="a"))
    )

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
