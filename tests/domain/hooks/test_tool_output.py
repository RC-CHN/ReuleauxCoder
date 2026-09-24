from types import SimpleNamespace
from pathlib import Path
import hashlib
import random
import tracemalloc

import pytest

from reuleauxcoder.extensions.hooks.builtin.tool_output import (
    ToolOutputTruncationHook,
    _line_summary,
    _retain_text,
)
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.builtin.history import (
    ArtifactReadTool,
    HistoryReadTool,
    HistorySearchTool,
)
from reuleauxcoder.infrastructure.persistence.session_paths import (
    session_path_component,
)
from reuleauxcoder.domain.history import HistoryLedger


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


def _legacy_retention(text, max_lines, max_chars, strategy):
    """Compatibility oracle for the previous whole-output algorithm."""
    lines = text.splitlines()
    if strategy is ToolRetentionStrategy.TAIL:
        return "\n".join(lines[-max_lines:])[-max_chars:].lstrip()
    if strategy is ToolRetentionStrategy.HEAD_TAIL:
        head = max(1, (max_lines + 1) // 2)
        tail = max_lines - head
        selected = "\n".join(lines[:head] + (lines[-tail:] if tail else []))
        if len(selected) <= max_chars:
            return selected
        head_chars = max(1, (max_chars + 1) // 2)
        tail_chars = max_chars - head_chars
        if not tail_chars:
            return selected[:head_chars].rstrip()
        return selected[:head_chars].rstrip() + "\n" + selected[-tail_chars:].lstrip()
    return "\n".join(lines[:max_lines])[:max_chars].rstrip()


@pytest.mark.parametrize("strategy", list(ToolRetentionStrategy))
@pytest.mark.parametrize("max_lines,max_chars", [(1, 1), (2, 3), (5, 16), (120, 12000)])
def test_bounded_retention_matches_previous_splitlines_semantics(
    strategy, max_lines, max_chars
):
    rng = random.Random(17)
    samples = ["", "\n", "\r\n", "a\r\nb\r\n", "x" * 100000]
    samples += [
        "".join(
            rng.choices(
                "abc 你好\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029", k=rng.randrange(200)
            )
        )
        for _ in range(150)
    ]
    for source in samples:
        assert _line_summary(source)[0] == len(source.splitlines())
        assert _retain_text(
            source, max_lines=max_lines, max_chars=max_chars, strategy=strategy
        ) == _legacy_retention(source, max_lines, max_chars, strategy)


@pytest.mark.parametrize("many_lines", [False, True], ids=["long-line", "many-lines"])
def test_projection_memory_is_bounded_by_retained_output(many_lines):
    source = "line\n" * 500000 if many_lines else "x" * (8 * 1024 * 1024)
    hook = ToolOutputTruncationHook(
        max_chars=8000, max_lines=120, store_full_output=False
    )
    ctx = _ctx("/tmp/output.log", source)
    ctx.outcome = ToolOutcome(
        content=source,
        model_content=source,
        retention_hint=ToolRetentionHint(strategy=ToolRetentionStrategy.HEAD_TAIL),
    )
    tracemalloc.start()
    try:
        result = hook.run(ctx)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024
    assert result.outcome.content == source
    assert result.outcome.truncation.original_lines == len(source.splitlines())


@pytest.mark.parametrize("session_id", [None, "archive-session"])
def test_chunked_archive_preserves_utf8_bytes_and_checksum(tmp_path, session_id):
    hook = ToolOutputTruncationHook(
        max_chars=20,
        max_lines=2,
        store_full_output=True,
        store_dir=str(tmp_path),
        sessions_dir=str(tmp_path),
    )
    source = "你好\r\n🙂\u2028" * 40000
    ctx = _ctx("/tmp/output.log", source)
    ctx.session_id = session_id
    ctx.outcome = ToolOutcome(content=source, model_content=source)
    result = hook.run(ctx)
    reference = result.outcome.archive_reference
    path = (
        tmp_path / session_id / "artifacts" / reference.path
        if session_id
        else Path(reference.path)
    )
    encoded = source.encode("utf-8")
    assert path.read_bytes() == encoded
    assert reference.size_bytes == len(encoded)
    assert reference.checksum_sha256 == hashlib.sha256(encoded).hexdigest()


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
    ctx.session_id = "remote:peer:session"

    out = hook.run(ctx)

    assert out.outcome is not None
    assert out.outcome.archive_reference is not None
    artifact_ref = out.outcome.archive_reference.path
    assert out.outcome.archive_reference.checksum_sha256 is not None
    assert out.outcome.archive_reference.size_bytes == len(source.encode("utf-8"))
    artifact = (
        tmp_path / session_path_component(ctx.session_id) / "artifacts" / artifact_ref
    )
    assert artifact.read_bytes() == source.encode("utf-8")
    assert f'artifact_read(artifact_ref="{artifact_ref}")' in out.result
    reader = ArtifactReadTool()
    reader._agent_config = SimpleNamespace(session_dir=str(tmp_path))
    assert reader.execute(artifact_ref, session_id=ctx.session_id).content == source
    # Reused provider call IDs must not overwrite the first outcome's archive.
    again = _ctx("/tmp/output.log", source + "\nsecond result")
    again.session_id = ctx.session_id
    second = hook.run(again)
    assert second.outcome.archive_reference.path != artifact_ref
    assert reader.execute(artifact_ref, session_id=ctx.session_id).content == source


def test_history_pages_keep_their_content_and_cursors_through_output_hooks(tmp_path):
    ledger = HistoryLedger(
        session_id="session", sink_path=tmp_path / "session" / "events.jsonl"
    )
    ledger.append_message(
        {"role": "user", "content": "original text " * 3000}, source="user"
    )
    hook = ToolOutputTruncationHook(
        max_chars=100, max_lines=2, store_full_output=True, sessions_dir=str(tmp_path)
    )
    for tool, arguments in (
        (HistoryReadTool(), {}),
        (HistorySearchTool(), {"pattern": "original"}),
    ):
        tool._agent_config = SimpleNamespace(session_dir=str(tmp_path))
        tool.bind_agent(SimpleNamespace(current_session_id="session"))
        outcome = tool.execute(**arguments)
        assert len(outcome.model_text) > hook.max_chars
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(id="query", name=tool.name, arguments=arguments),
            outcome=outcome,
            result=outcome.model_text,
            session_id="session",
        )
        processed = hook.run(context)
        assert processed.outcome is outcome
        assert processed.result == outcome.model_text
    assert not (tmp_path / "session" / "artifacts").exists()


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
