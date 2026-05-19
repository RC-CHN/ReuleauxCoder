"""Tests for the FIFO notes store."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from reuleauxcoder.infrastructure.persistence.notes_store import (
    read_notes,
    render_notes,
    write_note,
)


def _empty_notes(workspace: Path, global_: Path) -> tuple[Path, Path]:
    """Create empty notes files for test isolation."""
    workspace.parent.mkdir(parents=True, exist_ok=True)
    global_.parent.mkdir(parents=True, exist_ok=True)
    for p in (workspace, global_):
        with open(p, "w") as f:
            json.dump([], f)
    return workspace, global_


@patch("reuleauxcoder.infrastructure.persistence.notes_store._notes_path")
def test_write_and_read(mock_path):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_notes.json"
        mock_path.return_value = p

        assert read_notes() == []

        write_note("hello world")
        entries = read_notes()
        assert len(entries) == 1
        assert entries[0]["content"] == "hello world"
        assert "ts" in entries[0]


@patch("reuleauxcoder.infrastructure.persistence.notes_store._notes_path")
def test_fifo_trim(mock_path):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_notes.json"
        mock_path.return_value = p

        for i in range(10):
            write_note(f"note {i}", max_entries=5)

        entries = read_notes()
        assert len(entries) == 5
        assert entries[0]["content"] == "note 5"
        assert entries[-1]["content"] == "note 9"


@patch("reuleauxcoder.infrastructure.persistence.notes_store._notes_path")
def test_render_notes(mock_path):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_notes.json"
        mock_path.return_value = p

        write_note("style: prefer async/await")
        write_note("auth: JWT RS256")

        result = render_notes()
        assert result is not None
        assert "prefer async/await" in result
        assert "JWT RS256" in result


@patch("reuleauxcoder.infrastructure.persistence.notes_store._notes_path")
def test_render_empty(mock_path):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test_notes.json"
        mock_path.return_value = p
        assert render_notes() is None
