from pathlib import Path

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcomeStatus,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.diff import build_tool_diff
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool


def _backend(root: Path) -> LocalToolBackend:
    return LocalToolBackend(ExecutionContext(cwd=str(root), workspace_root=str(root)))


def test_write_and_edit_return_structured_unbounded_diffs(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    write = WriteFileTool(backend).execute("demo.txt", "alpha\nbeta\n")

    assert write.status is ToolOutcomeStatus.SUCCEEDED
    assert write.summary == "Wrote 2 lines to demo.txt"
    assert write.diff is not None
    assert write.diff.path == str(tmp_path / "demo.txt")
    assert "+alpha" in write.diff.unified
    assert write.diff.additions == 2
    assert write.diff.deletions == 0
    assert write.diff.original_chars == 0

    edit = EditFileTool(backend).execute("demo.txt", "beta", "gamma")

    assert edit.status is ToolOutcomeStatus.SUCCEEDED
    assert edit.diff is not None
    assert "-beta" in edit.diff.unified
    assert "+gamma" in edit.diff.unified
    assert edit.diff.additions == 1
    assert edit.diff.deletions == 1
    assert edit.diff.original_chars == len("alpha\nbeta\n")
    assert edit.model_text.startswith("Edited demo.txt\n--- a/")
    assert write.metadata["show_diff_by_default"] is True
    assert edit.metadata["show_diff_by_default"] is True


def test_diff_is_stable_across_platform_newline_encodings() -> None:
    lf = build_tool_diff("alpha\nbeta\n", "alpha\ngamma\n", "demo.txt")
    crlf = build_tool_diff("alpha\r\nbeta\r\n", "alpha\r\ngamma\r\n", "demo.txt")

    assert crlf == lf
    assert "\r" not in crlf.unified


def test_read_returns_full_model_content_and_compact_ui_summary(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    (tmp_path / "demo.txt").write_text("alpha\nbeta\ngamma\n")

    outcome = ReadFileTool(backend).execute("demo.txt", offset=2, limit=2)

    assert outcome.model_text == "2\tbeta\n3\tgamma"
    assert outcome.summary == "Read lines 2-3 of 3 (10 chars) from demo.txt"
    assert outcome.metadata["line_count"] == 2
    assert outcome.metadata["character_count"] == 10
    assert outcome.retention_hint.strategy is ToolRetentionStrategy.HEAD
    assert outcome.retention_hint.anchor_line == 2


def test_invalid_edit_has_explicit_failed_status(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    (tmp_path / "demo.txt").write_text("same")

    outcome = EditFileTool(backend).execute("demo.txt", "same", "same")

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "must differ" in outcome.model_text


def test_file_tools_build_canonical_approval_subjects_without_reading_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    backend = _backend(root)
    write = WriteFileTool(backend)
    edit = EditFileTool(backend)

    assert write.approval_subjects({"file_path": "src/new.py"}) == (
        "src/new.py",
    )
    assert edit.approval_subjects(
        {
            "file_path": str(root / "src" / "existing.py"),
            "old_string": "old",
            "new_string": "new",
        }
    ) == ("src/existing.py",)
    assert not (root / "src").exists()


def test_external_file_approval_subject_remains_an_absolute_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "external" / "new.py"
    write = WriteFileTool(_backend(root))

    assert write.approval_subjects({"file_path": str(target)}) == (
        target.resolve().as_posix(),
    )
