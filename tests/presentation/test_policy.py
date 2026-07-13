from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.presentation.policy import (
    PresentationPolicy,
    ToolOutputMode,
    Verbosity,
)


def test_preview_does_not_mutate_model_text() -> None:
    text = "\n".join(f"line-{i}" for i in range(30))
    outcome = ToolOutcome.from_legacy(text)
    policy = PresentationPolicy(
        tool_output_mode=ToolOutputMode.PREVIEW,
        tool_preview_chars=80,
        tool_preview_lines=3,
    )

    preview = policy.tool_preview(outcome)

    assert "output folded" in preview
    assert "line-0" in preview
    assert "line-29" in preview
    assert outcome.model_text == text


def test_summary_mode_bounds_legacy_tool_without_explicit_summary() -> None:
    text = "\n".join(f"read-line-{i}" for i in range(100))
    policy = PresentationPolicy(
        tool_output_mode=ToolOutputMode.SUMMARY,
        tool_preview_chars=120,
        tool_preview_lines=6,
    )

    preview = policy.tool_preview(ToolOutcome.from_legacy(text))

    assert "read-line-0" in preview
    assert "read-line-99" in preview
    assert "output folded" in preview
    assert len(preview) < len(text)


def test_summary_mode_prefers_structured_summary() -> None:
    outcome = ToolOutcome(summary="Wrote 500 lines", content="x" * 20_000)
    policy = PresentationPolicy(tool_output_mode=ToolOutputMode.SUMMARY)

    assert policy.tool_preview(outcome) == "Wrote 500 lines"


def test_summary_mode_renders_review_diff_for_write_and_edit() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import ToolDiff

    policy = PresentationPolicy(tool_output_mode=ToolOutputMode.SUMMARY)
    diff = ToolDiff(path="demo.txt", unified="--- a/demo\n+++ b/demo\n-x\n+y")
    for operation in ("write", "edit"):
        outcome = ToolOutcome(
            summary=f"{operation.title()} demo.txt",
            diff=diff,
            metadata={"operation": operation, "show_diff_by_default": True},
        )
        assert policy.tool_diff_preview(outcome) == diff.unified


def test_summary_mode_does_not_render_unrequested_diff() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import ToolDiff

    policy = PresentationPolicy(tool_output_mode=ToolOutputMode.SUMMARY)
    outcome = ToolOutcome(
        summary="Generated artifact",
        diff=ToolDiff(path="demo.txt", unified="+generated"),
    )

    assert policy.tool_diff_preview(outcome) == ""


def test_summary_mode_does_not_repeat_identical_reviewed_diff() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import ToolDiff

    policy = PresentationPolicy(tool_output_mode=ToolOutputMode.SUMMARY)
    outcome = ToolOutcome(
        summary="Edited demo.txt",
        diff=ToolDiff(path="demo.txt", unified="--- a/demo\n+++ b/demo\n-x\n+y"),
        metadata={"show_diff_by_default": True, "diff_reviewed": True},
    )

    assert policy.tool_diff_preview(outcome) == ""


def test_debug_preview_is_unbounded() -> None:
    text = "x" * 5_000
    policy = PresentationPolicy(
        verbosity=Verbosity.DEBUG,
        tool_output_mode=ToolOutputMode.FULL,
        tool_preview_chars=10,
    )

    assert policy.tool_preview(ToolOutcome.from_legacy(text)) == text


def test_errors_mode_suppresses_success_but_keeps_failure() -> None:
    policy = PresentationPolicy(tool_output_mode=ToolOutputMode.ERRORS)

    assert policy.tool_preview(ToolOutcome.from_legacy("ok")) == ""
    assert policy.tool_preview(ToolOutcome.from_legacy("bad", success=False)) == "bad"


def test_timeout_preview_shows_latest_five_lines_and_system_footer() -> None:
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.TIMED_OUT,
        stdout="\n".join(f"line-{index}" for index in range(10)),
        content="[system] timed out here",
    )

    preview = PresentationPolicy().tool_preview(outcome)

    assert "line-4" not in preview
    assert "line-5" in preview
    assert "line-9" in preview
    assert preview.endswith("[system] timed out here")
    assert "line-0" in outcome.model_text


def test_successful_tail_tool_commits_latest_five_lines_and_status() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import (
        ToolRetentionHint,
        ToolRetentionStrategy,
    )

    outcome = ToolOutcome(
        summary="Command completed · 5.39s",
        stdout="\n".join(f"line-{index}" for index in range(1, 51)),
        retention_hint=ToolRetentionHint(strategy=ToolRetentionStrategy.TAIL),
    )

    preview = PresentationPolicy().tool_preview(outcome)

    assert "line-45" not in preview
    for index in range(46, 51):
        assert f"line-{index}" in preview
    assert preview.endswith("└ Command completed · 5.39s")
    assert "line-1" in outcome.model_text
