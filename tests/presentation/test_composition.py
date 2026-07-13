from reuleauxcoder.presentation.composition import compose_transcript
from reuleauxcoder.presentation.models import (
    AssistantCell,
    ToolCell,
    UserCell,
)


def test_turn_group_shows_one_assistant_label_across_tool_continuation() -> None:
    cells = (
        UserCell(id="u1", text="question", group_id="turn-1"),
        AssistantCell(id="a1", text="before", group_id="turn-1"),
        ToolCell(
            id="t1",
            tool_call_id="tc1",
            name="read_file",
            arguments={},
            group_id="turn-1",
        ),
        AssistantCell(id="a2", text="after", group_id="turn-1"),
        UserCell(id="u2", text="next", group_id="turn-2"),
        AssistantCell(id="a3", text="answer", group_id="turn-2"),
    )

    placements = compose_transcript(cells)

    assert [item.begins_turn for item in placements] == [
        False,
        False,
        False,
        False,
        True,
        False,
    ]
    assert [item.show_assistant_label for item in placements] == [
        False,
        True,
        False,
        False,
        False,
        True,
    ]


def test_ungrouped_assistant_cells_remain_independently_labeled() -> None:
    placements = compose_transcript(
        (
            AssistantCell(id="a1", text="one"),
            AssistantCell(id="a2", text="two"),
        )
    )

    assert all(item.show_assistant_label for item in placements)
