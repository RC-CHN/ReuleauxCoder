"""Tests for the durable two-scope notes store."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from reuleauxcoder.infrastructure.persistence.notes_store import (
    NoteStore,
    NotesStoreError,
)


def _store(tmp_path: Path, **kwargs) -> NoteStore:
    return NoteStore(
        tmp_path / "workspace",
        home_dir=tmp_path / "home",
        **kwargs,
    )


def test_workspace_and_global_are_independent_stores(tmp_path: Path) -> None:
    store = _store(tmp_path, workspace_max=2, global_max=1)

    first = store.write("workspace one", scope="workspace")
    store.write("workspace two", scope="workspace")
    store.write("workspace three", scope="workspace")
    global_first = store.write("global one", scope="global")

    workspace = store.read("workspace")
    global_notes = store.read("global")
    assert [entry.content for entry in workspace] == [
        "workspace two",
        "workspace three",
    ]
    assert [entry.content for entry in global_notes] == ["global one"]
    assert first.id.startswith("wn_")
    assert global_first.id.startswith("gn_")
    assert store.path_for("workspace") != store.path_for("global")


def test_edit_preserves_identity_and_delete_requires_matching_scope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    original = store.write("old content", scope="workspace")

    assert store.edit(original.id, "new content", scope="global") is None
    updated = store.edit(original.id, "new content", scope="workspace")

    assert updated is not None
    assert updated.id == original.id
    assert updated.created_at == original.created_at
    assert updated.updated_at >= original.updated_at
    assert updated.content == "new content"
    assert store.delete(scope="global", note_id=original.id) is None
    assert store.delete(scope="workspace", note_id=original.id) == updated
    assert store.read("workspace") == []


def test_legacy_list_migrates_with_a_stable_scope_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.path_for("workspace")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps([{"content": "legacy", "ts": "2024-01-02T03:04:05+00:00"}]),
        encoding="utf-8",
    )

    first = store.read("workspace")[0]
    second = store.read("workspace")[0]
    assert first == second
    assert first.id.startswith("wn_")

    updated = store.edit(first.id, "migrated", scope="workspace")
    assert updated is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["scope"] == "workspace"
    assert payload["notes"][0]["id"] == first.id
    assert "ts" not in payload["notes"][0]


def test_corrupt_store_is_not_silently_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.path_for("workspace")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(NotesStoreError, match="invalid JSON"):
        store.write("must not replace the damaged file", scope="workspace")

    assert path.read_text(encoding="utf-8") == "{broken"


def test_render_guarantees_both_scopes_and_shows_stable_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = store.write("w" * 400, scope="workspace")
    global_note = store.write("global preference", scope="global")

    rendered = store.render(max_chars=240)

    assert rendered is not None
    assert len(rendered) <= 240
    assert "Workspace notes" in rendered
    assert "Global notes" in rendered
    assert workspace.id in rendered
    assert global_note.id in rendered


def test_concurrent_writes_are_serialized_and_capped(tmp_path: Path) -> None:
    store = _store(tmp_path, workspace_max=8)
    threads = [
        threading.Thread(
            target=store.write,
            args=(f"note {index}",),
            kwargs={"scope": "workspace"},
        )
        for index in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = store.read("workspace")
    assert len(entries) == 8
    assert len({entry.id for entry in entries}) == 8
