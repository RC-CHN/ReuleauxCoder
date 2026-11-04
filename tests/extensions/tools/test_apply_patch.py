import hashlib
from pathlib import Path

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcomeStatus
from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.patch import ApplyPatchTool


def _backend(root: Path) -> LocalToolBackend:
    return LocalToolBackend(ExecutionContext(cwd=str(root), workspace_root=str(root)))


def test_apply_patch_updates_multiple_files_and_hunks(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha\nmiddle\nomega\n")
    (tmp_path / "b.txt").write_bytes(b"old\n")
    backend = _backend(tmp_path)
    expected = hashlib.sha256(b"alpha\nmiddle\nomega\n").hexdigest()
    patch = """*** Begin Patch
*** Update File: a.txt
@@
-alpha
+ALPHA
@@
-omega
+OMEGA
*** Update File: b.txt
@@
-old
+new
*** Add File: c.txt
+created
*** End Patch"""

    outcome = ApplyPatchTool(backend).execute(
        patch, expected_revisions={"a.txt": expected}
    )

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert (tmp_path / "a.txt").read_text() == "ALPHA\nmiddle\nOMEGA\n"
    assert (tmp_path / "b.txt").read_text() == "new\n"
    assert (tmp_path / "c.txt").read_text() == "created\n"
    assert outcome.metadata["file_count"] == 3
    assert len(outcome.metadata["resolved_paths"]) == 3
    assert len(outcome.metadata["file_results"]) == 3
    assert outcome.metadata["operation"] == "apply_patch"
    assert outcome.diff is None
    assert set(ApplyPatchTool(backend).approval_subjects({"patch": patch})) == {
        "a.txt", "b.txt", "c.txt"
    }


def test_expected_revision_mismatch_writes_nothing(tmp_path: Path) -> None:
    original = "alpha\n"
    (tmp_path / "a.txt").write_text(original, encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: a.txt
@@
-alpha
+changed
*** End Patch"""

    outcome = ApplyPatchTool(_backend(tmp_path)).execute(
        patch, expected_revisions={"a.txt": "0" * 64}
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "expected revision mismatch" in outcome.content
    assert (tmp_path / "a.txt").read_text() == original


def test_ambiguous_hunk_writes_nothing(tmp_path: Path) -> None:
    original = "same\nsame\n"
    (tmp_path / "a.txt").write_text(original, encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: a.txt
@@
-same
+changed
*** End Patch"""

    outcome = ApplyPatchTool(_backend(tmp_path)).execute(patch)

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "not unique" in outcome.content
    assert (tmp_path / "a.txt").read_text() == original


def test_mid_patch_race_reports_applied_failed_and_unattempted(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("old\n", encoding="utf-8")
    backend = _backend(tmp_path)
    workspace = backend.workspace
    original_write = workspace.write_text_verified

    def racing_write(path, content, *, expected_revision=None):
        if path == "b.txt":
            raise WorkspaceError(WorkspaceErrorCode.REVISION_CONFLICT, "external edit")
        return original_write(path, content, expected_revision=expected_revision)

    monkeypatch.setattr(workspace, "write_text_verified", racing_write)
    patch = """*** Begin Patch
*** Update File: a.txt
@@
-old
+new a
*** Update File: b.txt
@@
-old
+new b
*** Update File: c.txt
@@
-old
+new c
*** End Patch"""

    outcome = ApplyPatchTool(backend).execute(patch)

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.metadata["applied_files"] == ["a.txt"]
    assert outcome.metadata["failed_file"] == "b.txt"
    assert outcome.metadata["unattempted_files"] == ["c.txt"]
    assert (tmp_path / "a.txt").read_text() == "new a\n"
    assert (tmp_path / "b.txt").read_text() == "old\n"
    assert (tmp_path / "c.txt").read_text() == "old\n"
