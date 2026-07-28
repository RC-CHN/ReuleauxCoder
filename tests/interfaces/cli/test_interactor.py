from reuleauxcoder.interfaces.cli.interactor import CLIUIInteractor
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel
from reuleauxcoder.interfaces.interactions import (
    InputTextRequest,
    ReviewGrantOption,
    ReviewRequest,
)


def test_ctrl_c_review_cancels_and_breaks_the_partial_prompt_line(capsys) -> None:
    events = []
    bus = UIEventBus()
    bus.subscribe(events.append)

    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    interactor = CLIUIInteractor(bus, prompt_fn=interrupted)
    response = interactor.review(
        ReviewRequest(title="Approval", summary="Review this change")
    )

    assert response.approved is False
    assert response.cancelled is True
    assert response.reason == "approval interrupted"
    assert capsys.readouterr().out == "\n"
    assert events[-1].message == "Interrupted."
    assert events[-1].kind is UIEventKind.APPROVAL
    assert events[-1].level is UIEventLevel.WARNING


def test_review_accepts_codex_style_numbered_choices() -> None:
    answers = iter(("1", "2"))
    interactor = CLIUIInteractor(UIEventBus(), prompt_fn=lambda _prompt: next(answers))
    request = ReviewRequest(title="Approval", summary="Review this change")

    assert interactor.review(request).approved is True
    assert interactor.review(request).approved is False


def test_review_can_select_session_scope_or_deny_with_feedback() -> None:
    answers = iter(("s", "2", "f", "Use the adapter instead."))
    interactor = CLIUIInteractor(UIEventBus(), prompt_fn=lambda _prompt: next(answers))
    request = ReviewRequest(
        title="Approval",
        summary="Review this change",
        grant_options=(
            ReviewGrantOption("exact", "This file", "src/app.py"),
            ReviewGrantOption(
                "directory",
                "This directory",
                "src/**",
                broad=True,
            ),
        ),
    )

    granted = interactor.review(request)
    denied = interactor.review(request)

    assert granted.action == "allow_session"
    assert granted.selected_id == "directory"
    assert granted.approved is True
    assert denied.action == "deny"
    assert denied.reason == "Use the adapter instead."


def test_secret_text_uses_dedicated_masked_prompt() -> None:
    prompts = []
    interactor = CLIUIInteractor(
        UIEventBus(),
        prompt_fn=lambda _prompt: "ordinary",
        secret_prompt_fn=lambda prompt: prompts.append(prompt) or "  hidden value  ",
    )

    response = interactor.input_text(
        InputTextRequest(
            title="Secure input",
            prompt="Enter hidden text",
            secret=True,
        )
    )

    assert response.value == "  hidden value  "
    assert prompts == ["Enter hidden text: "]
