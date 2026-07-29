from pathlib import Path
from fnmatch import fnmatchcase
from functools import lru_cache
import os
import threading

import pytest

from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceMutationReceipt,
    WorkspaceMutationVerification,
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


def test_exact_external_path_grant_is_scoped_and_does_not_widen_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external.txt"
    other = tmp_path / "other.txt"
    workspace = LocalWorkspacePort(root)

    assert workspace.external_path(external) == external.resolve()
    assert workspace.external_path(root / "inside.txt") is None
    with pytest.raises(WorkspaceError) as before:
        workspace.write_text_atomic(external, "outside")
    assert before.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE

    with workspace.grant_external_path(external):
        workspace.write_text_atomic(external, "outside")
        assert workspace.read_text(external) == "outside"
        with pytest.raises(WorkspaceError) as unrelated:
            workspace.write_text_atomic(other, "not granted")
        assert unrelated.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE

    assert external.read_text() == "outside"
    with pytest.raises(WorkspaceError) as after:
        workspace.read_text(external)
    assert after.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE


def test_external_path_grant_does_not_leak_between_execution_threads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("outside")
    workspace = LocalWorkspacePort(root)
    granted = threading.Event()
    release = threading.Event()

    def hold_grant() -> None:
        with workspace.grant_external_path(external):
            assert workspace.read_text(external) == "outside"
            granted.set()
            release.wait(timeout=2)

    worker = threading.Thread(target=hold_grant)
    worker.start()
    assert granted.wait(timeout=1)
    try:
        with pytest.raises(WorkspaceError) as separate_context:
            workspace.read_text(external)
        assert separate_context.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE
    finally:
        release.set()
        worker.join(timeout=1)
    assert not worker.is_alive()


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


def test_snapshot_revision_distinguishes_missing_empty_and_raw_bytes(
    tmp_path: Path,
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    missing = workspace.snapshot_text("missing.txt")
    path = tmp_path / "file.txt"
    path.write_bytes(b"")
    empty = workspace.snapshot_text(path)
    path.write_bytes(b"\xff")
    first_invalid = workspace.snapshot_text(path)
    path.write_bytes(b"\xfe")
    second_invalid = workspace.snapshot_text(path)

    assert missing.content is None
    assert missing.revision.exists is False
    assert missing.revision.sha256 is None
    assert empty.content == ""
    assert empty.revision.exists is True
    assert empty.revision.size_bytes == 0
    assert empty.revision.sha256 is not None
    assert first_invalid.content == second_invalid.content == "\ufffd"
    assert not first_invalid.revision.same_content(second_invalid.revision)


def test_verified_write_reports_external_base_and_observed_result(
    tmp_path: Path,
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("approved")
    approved = workspace.snapshot_text(path).revision
    path.write_text("changed by editor")

    result = workspace.write_text_verified(
        path,
        "intended",
        expected_revision=approved,
    )

    assert result.old_content == "changed by editor"
    assert result.new_content == "intended"
    assert result.receipt.external_change_before_write is True
    assert (
        result.receipt.verification
        is WorkspaceMutationVerification.APPLIED_VERIFIED
    )
    assert result.receipt.atomic_replace is True
    assert result.receipt.observed_after is not None
    assert (
        result.receipt.observed_after.sha256
        == result.receipt.intended_after_sha256
    )
    assert path.read_text() == "intended"
    assert (
        WorkspaceMutationReceipt.from_dict(result.receipt.to_dict())
        == result.receipt
    )


def test_failed_write_receipt_confirms_unchanged_target(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("old")

    def fail_replace(source, target) -> None:  # noqa: ARG001
        raise OSError("replace failed")

    monkeypatch.setattr("reuleauxcoder.infrastructure.workspace.local.os.replace", fail_replace)

    with pytest.raises(WorkspaceError) as failed:
        workspace.write_text_verified(path, "new")

    receipt = failed.value.mutation_receipt
    assert receipt is not None
    assert receipt.verification is WorkspaceMutationVerification.FAILED_UNCHANGED
    assert receipt.atomic_replace is False
    assert receipt.observed_after is not None
    assert receipt.observed_after.same_content(receipt.before)
    assert path.read_text() == "old"


def test_failed_write_receipt_reports_intended_contents_already_applied(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("old")
    original_replace = os.replace

    def replace_then_fail(source, target) -> None:
        original_replace(source, target)
        raise OSError("replace result was not acknowledged")

    monkeypatch.setattr(
        "reuleauxcoder.infrastructure.workspace.local.os.replace",
        replace_then_fail,
    )

    with pytest.raises(WorkspaceError) as failed:
        workspace.write_text_verified(path, "new")

    receipt = failed.value.mutation_receipt
    assert receipt is not None
    assert (
        receipt.verification
        is WorkspaceMutationVerification.APPLIED_VERIFIED
    )
    assert receipt.atomic_replace is False
    assert receipt.observed_after is not None
    assert receipt.observed_after.sha256 == receipt.intended_after_sha256
    assert path.read_text() == "new"


def test_failed_write_receipt_reports_diverged_target(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("old")

    def change_then_fail(source, target) -> None:  # noqa: ARG001
        Path(target).write_text("external")
        raise OSError("replace raced")

    monkeypatch.setattr(
        "reuleauxcoder.infrastructure.workspace.local.os.replace",
        change_then_fail,
    )

    with pytest.raises(WorkspaceError) as failed:
        workspace.write_text_verified(path, "new")

    receipt = failed.value.mutation_receipt
    assert receipt is not None
    assert receipt.verification is WorkspaceMutationVerification.DIVERGED
    assert receipt.observed_after is not None
    assert not receipt.observed_after.same_content(receipt.before)
    assert path.read_text() == "external"


def test_successful_replace_with_unreadable_post_state_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("old")
    original_snapshot = workspace.snapshot_text
    calls = 0

    def snapshot(current_path):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise WorkspaceError(WorkspaceErrorCode.IO_ERROR, "cannot verify")
        return original_snapshot(current_path)

    monkeypatch.setattr(workspace, "snapshot_text", snapshot)

    result = workspace.write_text_verified(path, "new")

    assert result.receipt.verification is WorkspaceMutationVerification.UNKNOWN
    assert result.receipt.atomic_replace is True
    assert result.receipt.observed_after is None
    assert path.read_text() == "new"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_exact_edit_retries_against_latest_external_contents(
    tmp_path: Path, monkeypatch, line_ending: str
) -> None:
    workspace = LocalWorkspacePort(tmp_path)
    path = tmp_path / "file.txt"
    original = f"old{line_ending}"
    external = f"old{line_ending}external{line_ending}"
    updated = f"new{line_ending}external{line_ending}"
    path.write_bytes(original.encode("utf-8"))
    approved = workspace.snapshot_text(path).revision
    original_snapshot = workspace.snapshot_text
    calls = 0

    def snapshot(current_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(external.encode("utf-8"))
        return original_snapshot(current_path)

    monkeypatch.setattr(workspace, "snapshot_text", snapshot)

    result = workspace.replace_exact_verified(
        path,
        "old",
        "new",
        expected_revision=approved,
    )

    assert result.old_content == external
    assert result.new_content == updated
    assert result.receipt.external_change_before_write is True
    assert (
        result.receipt.verification
        is WorkspaceMutationVerification.APPLIED_VERIFIED
    )
    assert path.read_bytes() == updated.encode("utf-8")


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

    optimized = workspace.glob_paths("**/*.py", ".", max_entries=100, max_matches=2)
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
    reference = search_text_via_primitives(workspace, "needle", ".", **arguments)

    assert optimized == reference


def test_optimized_single_file_search_matches_primitive_reference(
    tmp_path: Path,
) -> None:
    target = tmp_path / "one.txt"
    target.write_text("needle one\nneedle two\n")
    workspace = LocalWorkspacePort(tmp_path)

    optimized = workspace.search_text("needle", target, max_matches=1)
    reference = search_text_via_primitives(workspace, "needle", target, max_matches=1)

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
