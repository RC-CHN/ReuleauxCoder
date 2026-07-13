from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.extensions.tools.builtin.notes import (
    DeleteNoteTool,
    EditNoteTool,
    WriteNoteTool,
)
from reuleauxcoder.infrastructure.persistence.notes_store import NoteStore


def _bind(tool, store: NoteStore):
    tool.bind_agent(SimpleNamespace(notes_store=store))
    return tool


def test_note_tools_round_trip_by_stable_id_and_explicit_scope(
    tmp_path: Path,
) -> None:
    store = NoteStore(tmp_path / "workspace", home_dir=tmp_path / "home")
    writer = _bind(WriteNoteTool(), store)
    editor = _bind(EditNoteTool(), store)
    deleter = _bind(DeleteNoteTool(), store)

    created = writer.execute("Prefer narrow commits", scope="global")
    note_id = store.read("global")[0].id
    assert note_id in created

    assert "No workspace note" in editor.execute(
        note_id, "Prefer atomic commits", "workspace"
    )
    assert note_id in editor.execute(note_id, "Prefer atomic commits", "global")
    assert store.read("global")[0].content == "Prefer atomic commits"

    assert note_id in deleter.execute("global", note_id=note_id)
    assert store.read("global") == []


def test_note_tools_are_internal_control_plane_mutations() -> None:
    for tool in (WriteNoteTool(), EditNoteTool(), DeleteNoteTool()):
        assert tool.effect_class == "control_plane_internal"
