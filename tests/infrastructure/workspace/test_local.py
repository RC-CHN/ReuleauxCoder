from pathlib import Path
from fnmatch import fnmatchcase
from functools import lru_cache

import pytest

from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    glob_paths_via_primitives,
    search_text_via_primitives,
)
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


def test_optimized_glob_matches_primitive_reference_exactly(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("visible")
    (tmp_path / ".hidden.py").write_text("hidden")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.py").write_text("deep")
    (nested / "plain.txt").write_text("plain")
    workspace = LocalWorkspacePort(tmp_path)

    optimized = workspace.glob_paths(
        "**/*.py", ".", max_entries=100, max_matches=2
    )
    reference = glob_paths_via_primitives(
        workspace,
        "**/*.py",
        ".",
        max_entries=100,
        max_matches=2,
    )

    assert optimized == reference


def test_optimized_glob_preserves_listing_truncation_order(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (tmp_path / name).write_text(name)
    workspace = LocalWorkspacePort(tmp_path)

    optimized = workspace.glob_paths("*.py", ".", max_entries=3, max_matches=10)
    reference = glob_paths_via_primitives(
        workspace, "*.py", ".", max_entries=3, max_matches=10
    )

    assert optimized == reference
    assert optimized.listing_truncated is True


def test_optimized_search_matches_primitive_reference_exactly(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.py").write_text("first\nneedle one\vneedle two\n")
    (tmp_path / "two.txt").write_text("needle ignored\n")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "three.py").write_text("needle hidden\n")
    skipped = tmp_path / ".git"
    skipped.mkdir()
    (skipped / "four.py").write_text("needle skipped\n")
    workspace = LocalWorkspacePort(tmp_path)
    arguments = {
        "include": "*.py",
        "exclude_dirs": (".git",),
        "max_files": 10,
        "max_matches": 3,
    }

    optimized = workspace.search_text("needle", ".", **arguments)
    reference = search_text_via_primitives(
        workspace, "needle", ".", **arguments
    )

    assert optimized == reference


def test_optimized_single_file_search_matches_primitive_reference(
    tmp_path: Path,
) -> None:
    target = tmp_path / "one.txt"
    target.write_text("needle one\nneedle two\n")
    workspace = LocalWorkspacePort(tmp_path)

    optimized = workspace.search_text("needle", target, max_matches=1)
    reference = search_text_via_primitives(
        workspace, "needle", target, max_matches=1
    )

    assert optimized == reference


@pytest.mark.parametrize(
    ("relative_path", "pattern"),
    [
        ("one.py", "*.py"),
        ("nested/one.py", "*.py"),
        ("one.py", "**/*.py"),
        ("nested/one.py", "**/*.py"),
        ("src/one.ts", "src/**/*.ts"),
        ("src/deep/one.ts", "src/**/*.ts"),
        ("src/deep/one.ts", "src/**"),
        ("src/demo1.py", "src/demo?.py"),
        ("src/demoa.py", "src/demo[ab].py"),
    ],
)
def test_precompiled_glob_preserves_legacy_match_semantics(
    relative_path: str, pattern: str
) -> None:
    from reuleauxcoder.domain.workspace import portable_glob_match

    assert portable_glob_match(relative_path, pattern) is _legacy_glob_match(
        relative_path, pattern
    )


def _legacy_glob_match(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(
        part for part in relative_path.replace("\\", "/").split("/") if part
    )
    pattern_parts = tuple(
        part for part in pattern.replace("\\", "/").split("/") if part
    )
    if not path_parts or not pattern_parts:
        return False

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)
