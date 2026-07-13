from dataclasses import replace

from reuleauxcoder.presentation.models import AssistantCell, TranscriptModel, UserCell


def test_transcript_caches_snapshot_until_source_changes() -> None:
    model = TranscriptModel()
    initial = model.cells

    assert initial is model.cells
    assert model.revision == 0

    model.append(UserCell(id="user:1", text="hello"))
    appended = model.cells

    assert appended is model.cells
    assert appended != initial
    assert model.revision == 1

    model.replace(replace(appended[0], text="hello again", revision=1))

    assert model.cells is model.cells
    assert model.cells[0].text == "hello again"
    assert model.revision == 2

    model.clear()

    assert model.cells == ()
    assert model.revision == 3


def test_transcript_retention_tracks_replacement_size_incrementally() -> None:
    model = TranscriptModel(max_cells=10, max_text_chars=8)
    model.append(UserCell(id="user:1", text="1234"))
    model.append(AssistantCell(id="assistant:1", text="5678"))

    model.replace(AssistantCell(id="assistant:1", text="567890", revision=1))

    assert [cell.id for cell in model.cells] == ["assistant:1"]
    assert model.get("user:1") is None
    assert model.get("assistant:1") is model.cells[0]

    evicted = model.append(UserCell(id="user:2", text="abc"))

    assert [cell.id for cell in evicted] == ["assistant:1"]
    assert [cell.id for cell in model.cells] == ["user:2"]
