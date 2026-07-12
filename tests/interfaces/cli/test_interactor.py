from reuleauxcoder.interfaces.cli.interactor import CLIUIInteractor
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel
from reuleauxcoder.interfaces.interactions import ReviewRequest


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
