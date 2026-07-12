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


def test_text_primitives_preserve_newline_bytes(tmp_path: Path) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_bytes(b"old\r\n")

    previous = workspace.write_text_atomic("file.txt", "new\n")

    assert previous == "old\r\n"
    assert path.read_bytes() == b"new\n"


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


def test_structured_list_is_recursive_bounded_and_hides_dotfiles(
    tmp_path: Path,
) -> None:
    (tmp_path / "visible.txt").write_text("visible")
    (tmp_path / ".hidden").write_text("hidden")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "code.py").write_text("print('ok')")
    workspace = LocalWorkspacePort(tmp_path)

    result = workspace.list_entries(
        ".", recursive=True, include_hidden=False, max_entries=2
    )

    assert result.truncated is True
    assert len(result.entries) == 2
    assert all(entry.name != ".hidden" for entry in result.entries)
    assert all(Path(entry.path).is_absolute() for entry in result.entries)


def test_structured_search_filters_files_and_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("needle one\nneedle two\n")
    (tmp_path / "two.txt").write_text("needle ignored\n")
    skipped = tmp_path / ".git"
    skipped.mkdir()
    (skipped / "hidden.py").write_text("needle hidden\n")
    workspace = LocalWorkspacePort(tmp_path)

    result = workspace.search_text(
        "needle",
        ".",
        include="*.py",
        exclude_dirs=(".git",),
        max_matches=1,
    )

    assert result.truncated is True
    assert len(result.matches) == 1
    assert result.matches[0].path == str(tmp_path / "one.py")
    assert result.matches[0].line_number == 1


def test_search_does_not_follow_file_symlink_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("secret needle")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    workspace = LocalWorkspacePort(tmp_path)

    result = workspace.search_text("secret", ".")

    assert result.matches == ()
