import pytest

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)


def test_legacy_outcome_keeps_model_and_display_text_unbounded() -> None:
    text = "x" * 10_000
    outcome = ToolOutcome.from_legacy(text)

    assert outcome.model_text == text
    assert outcome.display_text == text
    assert outcome.success is True


def test_failed_outcome_can_carry_stable_error_kind() -> None:
    outcome = ToolOutcome.from_legacy(
        "denied", success=False, error_kind=ToolErrorKind.DENIED
    )

    assert outcome.error_kind is ToolErrorKind.DENIED


def test_successful_outcome_rejects_error_kind() -> None:
    with pytest.raises(ValueError):
        ToolOutcome(
            status=ToolOutcomeStatus.SUCCEEDED,
            content="bad",
            error_kind=ToolErrorKind.INTERNAL,
        )


def test_model_and_ui_projections_are_independent() -> None:
    outcome = ToolOutcome(summary="short", content="full details")
    bounded = outcome.with_model_projection("model limit")

    assert bounded.model_text == "model limit"
    assert bounded.ui_text(include_details=False) == "short"
    assert bounded.ui_text(include_details=True) == "full details"
