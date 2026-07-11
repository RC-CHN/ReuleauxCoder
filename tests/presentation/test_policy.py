from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.presentation.policy import PresentationPolicy, Verbosity


def test_preview_does_not_mutate_model_text() -> None:
    text = "\n".join(f"line-{i}" for i in range(30))
    outcome = ToolOutcome.from_legacy(text)
    policy = PresentationPolicy(tool_preview_chars=80, tool_preview_lines=3)

    preview = policy.tool_preview(outcome)

    assert "hidden" in preview
    assert outcome.model_text == text


def test_debug_preview_is_unbounded() -> None:
    text = "x" * 5_000
    policy = PresentationPolicy(verbosity=Verbosity.DEBUG, tool_preview_chars=10)

    assert policy.tool_preview(ToolOutcome.from_legacy(text)) == text
