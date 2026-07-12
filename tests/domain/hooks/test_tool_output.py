from pathlib import Path

from reuleauxcoder.domain.hooks.builtin.tool_output import ToolOutputTruncationHook
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall


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


def test_tool_output_retains_structured_source_while_bounding_model_projection() -> None:
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
