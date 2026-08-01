from types import SimpleNamespace
from pathlib import Path

from reuleauxcoder.domain.hooks.builtin.tool_output import ToolOutputTruncationHook
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.builtin.history import ArtifactReadTool


def _ctx(
    file_path: str, result: str, *, override: bool = False
) -> AfterToolExecuteContext:
    return AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="1",
            name="read_file",
            arguments={"file_path": file_path, "override": override},
        ),
        result=result,
        round_index=1,
    )


def test_tool_output_truncates_regular_read_file_output() -> None:
    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"

    ctx = _ctx("/tmp/notes.md", long_text)
    out = hook.run(ctx)

    assert "[truncated]" in out.result


def test_tool_output_retains_structured_source_while_bounding_model_projection() -> (
    None
):
    hook = ToolOutputTruncationHook(max_chars=12, max_lines=2, store_full_output=False)
    source = "line1\nline2\nline3"
    ctx = _ctx("/tmp/notes.md", source)
    ctx.outcome = ToolOutcome(summary="read notes", content=source)

    out = hook.run(ctx)

    assert out.outcome is not None
    assert out.outcome.content == source
    assert out.outcome.summary == "read notes"
    assert out.outcome.truncation is not None
    assert out.outcome.truncation.original_chars == len(source)
    assert "[truncated]" in out.outcome.model_text
    assert out.outcome.ui_text(include_details=True) == source


def test_tool_output_bypasses_truncation_for_workspace_skills_markdown(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_md = workspace / ".rcoder" / "skills" / "demo" / "SKILL.md"

    ctx = _ctx(str(skill_md), long_text)
    out = hook.run(ctx)

    assert out.result == long_text


def test_tool_output_bypasses_truncation_for_global_skills_markdown(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Path.home() uses HOME on Unix, USERPROFILE on Windows.
    # Patch it directly so the test is platform-agnostic.
    monkeypatch.setattr(Path, "home", lambda: home)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_md = home / ".rcoder" / "skills" / "demo" / "guide.md"

    ctx = _ctx(str(skill_md), long_text)
    out = hook.run(ctx)

    assert out.result == long_text


def test_tool_output_does_not_bypass_non_markdown_under_skills(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    hook = ToolOutputTruncationHook(max_chars=20, max_lines=2, store_full_output=False)
    long_text = "line1\nline2\nline3\nline4"
    skill_txt = workspace / ".rcoder" / "skills" / "demo" / "notes.txt"

    ctx = _ctx(str(skill_txt), long_text)
    out = hook.run(ctx)

    assert "[truncated]" in out.result


def test_tool_output_retains_tail_when_outcome_requests_it() -> None:
    hook = ToolOutputTruncationHook(max_chars=100, max_lines=3, store_full_output=False)
    source = "\n".join(f"line-{index}" for index in range(10))
    ctx = _ctx("/tmp/output.log", source)
    ctx.outcome = ToolOutcome(
        content=source,
        retention_hint=ToolRetentionHint(strategy=ToolRetentionStrategy.TAIL),
    )

    out = hook.run(ctx)

    assert "Showing last 3 retained lines" in out.result
    assert "line-7\nline-8\nline-9" in out.result
    assert "line-0" not in out.result
    assert out.outcome.truncation.strategy == "tail"


def test_tool_output_head_retention_reports_source_anchor() -> None:
    hook = ToolOutputTruncationHook(max_chars=100, max_lines=2, store_full_output=False)
    source = "\n".join(f"source-{index}" for index in range(10, 20))
    ctx = _ctx("/tmp/source.py", source)
    ctx.outcome = ToolOutcome(
        content=source,
        retention_hint=ToolRetentionHint(
            strategy=ToolRetentionStrategy.HEAD, anchor_line=11
        ),
    )

    out = hook.run(ctx)

    assert "Showing first 2 retained lines from source line 11" in out.result
    assert "source-10\nsource-11" in out.result
    assert "source-19" not in out.result


def test_tool_output_archive_is_session_scoped_and_model_recoverable(
    tmp_path: Path,
) -> None:
    hook = ToolOutputTruncationHook(
        max_chars=20,
        max_lines=2,
        store_full_output=True,
        sessions_dir=str(tmp_path),
    )
    source = "\n".join(f"line-{index}" for index in range(20))
    ctx = _ctx("/tmp/output.log", source)
    ctx.session_id = "session_test"

    out = hook.run(ctx)

    assert out.outcome is not None
    assert out.outcome.archive_reference is not None
    assert out.outcome.archive_reference.path == "tools/1.txt"
    assert out.outcome.archive_reference.checksum_sha256 is not None
    assert out.outcome.archive_reference.size_bytes == len(source.encode("utf-8"))
    artifact = tmp_path / "session_test" / "artifacts" / "tools" / "1.txt"
    assert artifact.read_text(encoding="utf-8") == source
    assert 'artifact_read(artifact_ref="tools/1.txt")' in out.result


def test_archived_output_can_be_paged_without_recursive_archiving(
    tmp_path: Path,
) -> None:
    hook = ToolOutputTruncationHook(
        max_chars=20,
        max_lines=2,
        store_full_output=True,
        sessions_dir=str(tmp_path),
    )
    source = "alpha-中文-beta-" * 20
    archived_context = _ctx("/tmp/output.log", source)
    archived_context.session_id = "session_test"
    archived = hook.run(archived_context)
    artifact_ref = archived.outcome.archive_reference.path
    artifact_dir = tmp_path / "session_test" / "artifacts" / "tools"
    original_artifacts = set(artifact_dir.iterdir())

    tool = ArtifactReadTool()
    tool._agent_config = SimpleNamespace(session_dir=str(tmp_path))
    tool.bind_agent(SimpleNamespace(current_session_id="session_test"))
    pages: list[str] = []
    offset = 0
    while True:
        outcome = tool.execute(artifact_ref, offset=offset, limit=17)
        artifact_context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id=f"read-{offset}",
                name="artifact_read",
                arguments={
                    "artifact_ref": artifact_ref,
                    "offset": offset,
                    "limit": 17,
                },
            ),
            result=outcome.model_text,
            outcome=outcome,
            round_index=2,
            session_id="session_test",
        )
        processed = hook.run(artifact_context)
        assert processed.outcome is outcome
        assert "[truncated]" not in processed.result
        pages.append(outcome.content or "")
        next_offset = outcome.metadata["next_offset"]
        if next_offset is None:
            break
        offset = int(next_offset)

    assert "".join(pages) == source
    assert set(artifact_dir.iterdir()) == original_artifacts
