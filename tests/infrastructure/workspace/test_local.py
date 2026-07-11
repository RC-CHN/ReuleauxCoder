from pathlib import Path

import pytest

from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


def test_relative_and_absolute_paths_are_confined(tmp_path: Path) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    inside = tmp_path / "inside.txt"
    inside.write_text("ok")

    assert workspace.read_text("inside.txt") == "ok"
    assert workspace.read_text(inside) == "ok"
    with pytest.raises(WorkspaceError) as escaped:
        workspace.read_text(tmp_path.parent / "outside.txt")
    assert escaped.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE
    with pytest.raises(WorkspaceError):
        workspace.resolve("../escape.txt")


def test_atomic_write_returns_previous_content(tmp_path: Path) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("old")

    previous = workspace.write_text_atomic("file.txt", "new")

    assert previous == "old"
    assert path.read_text() == "new"


def test_exact_replace_is_atomic_and_requires_unique_match(tmp_path: Path) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("one two")

    old, new = workspace.replace_exact_atomic("file.txt", "two", "three")

    assert old == "one two"
    assert new == "one three"
    assert path.read_text() == "one three"

    path.write_text("x x")
    with pytest.raises(WorkspaceError) as duplicate:
        workspace.replace_exact_atomic("file.txt", "x", "y")
    assert duplicate.value.code is WorkspaceErrorCode.NOT_UNIQUE
    assert path.read_text() == "x x"
