from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool, _glob_full_match
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool


def _backend(tmp_path) -> LocalToolBackend:
    return LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
    )


def test_glob_keeps_matches_for_model_but_summarizes_ui(tmp_path) -> None:
    (tmp_path / "one.py").write_text("one\n")
    (tmp_path / "two.py").write_text("two\n")

    outcome = GlobTool(backend=_backend(tmp_path)).execute(
        pattern="*.py", path=str(tmp_path)
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.summary == "Found 2 files matching *.py"
    assert "one.py" in outcome.model_text
    assert outcome.metadata["match_count"] == 2


def test_glob_full_match_has_portable_recursive_segment_semantics() -> None:
    assert _glob_full_match("one.py", "*.py") is True
    assert _glob_full_match("nested/one.py", "*.py") is False
    assert _glob_full_match("one.py", "**/*.py") is True
    assert _glob_full_match("nested/one.py", "**/*.py") is True
    assert _glob_full_match("src/one.ts", "src/**/*.ts") is True
    assert _glob_full_match("other/src/one.ts", "src/**/*.ts") is False


def test_grep_keeps_matching_lines_for_model_but_summarizes_ui(tmp_path) -> None:
    (tmp_path / "one.py").write_text("needle\n")
    (tmp_path / "two.py").write_text("needle again\n")

    outcome = GrepTool(backend=_backend(tmp_path)).execute(
        pattern="needle", path=str(tmp_path), include="*.py"
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.summary == "Found 2 matches across 2 files"
    assert "needle again" in outcome.model_text
    assert outcome.metadata["file_count"] == 2


def test_list_keeps_entries_for_model_but_summarizes_ui(tmp_path) -> None:
    (tmp_path / "one.py").write_text("one\n")
    (tmp_path / "two.py").write_text("two\n")

    outcome = ListFileTool(backend=_backend(tmp_path)).execute(
        path=str(tmp_path), long=False
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.summary == f"Listed 2 entries in {tmp_path}"
    assert "one.py" in outcome.model_text
    assert outcome.metadata["entry_count"] == 2
