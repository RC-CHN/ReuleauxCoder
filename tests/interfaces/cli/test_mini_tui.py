import threading
import time
from types import SimpleNamespace

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.approval import ApprovalSection, ApprovalSectionKind
from reuleauxcoder.domain.runtime.events import agent_event_to_runtime_event
from reuleauxcoder.interfaces.cli.mini_tui import (
    MiniTUIEventAdapter,
    MiniTUIInteractor,
    _interaction_response,
)
from reuleauxcoder.interfaces.events import RuntimeEventPayload, UIEvent, UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import (
    ChoiceItem,
    ChooseOneRequest,
    ConfirmRequest,
    InputTextRequest,
    ReviewRequest,
)


def test_event_adapter_projects_user_and_execution_state() -> None:
    adapter = MiniTUIEventAdapter()
    runtime = agent_event_to_runtime_event(
        AgentEvent.chat_start("fix the renderer"), agent_id="main"
    )
    adapter.on_ui_event(
        UIEvent.info(
            "turn_started",
            kind=UIEventKind.AGENT,
            payload=RuntimeEventPayload(runtime),
        )
    )

    fragments = adapter.transcript_fragments()
    rendered = "".join(fragment[1] for fragment in fragments)
    assert "fix the renderer" in rendered
    assert adapter.execution.state.runtime_state == "running"
    assert "MAIN" in "\n".join(adapter.panel_lines(100))


def test_interactor_blocks_worker_until_bottom_pane_response() -> None:
    interactor = MiniTUIInteractor(UIEventBus())
    result = []

    worker = threading.Thread(
        target=lambda: result.append(interactor.review(ReviewRequest("Edit", "diff")))
    )
    worker.start()
    deadline = time.monotonic() + 1
    while interactor.active_request is None and time.monotonic() < deadline:
        time.sleep(0.005)

    assert interactor.active_request is not None
    assert interactor.submit("")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert result[0].approved


def test_interactor_cancel_resolves_protocol_response() -> None:
    interactor = MiniTUIInteractor(UIEventBus())
    result = []
    request = ReviewRequest("Edit", "diff")
    worker = threading.Thread(target=lambda: result.append(interactor.review(request)))
    worker.start()
    deadline = time.monotonic() + 1
    while interactor.active_request is None and time.monotonic() < deadline:
        time.sleep(0.005)
    interactor.cancel(request.request_id)
    worker.join(timeout=1)
    assert result[0].cancelled
    assert result[0].reason == "interaction cancelled"


def test_review_details_toggle_and_scroll_do_not_resolve_request() -> None:
    interactor = MiniTUIInteractor(UIEventBus())
    result = []
    request = ReviewRequest(
        "Edit",
        "Review diff",
        sections=(
            ApprovalSection(
                "diff",
                "DIFF",
                ApprovalSectionKind.DIFF,
                "\n".join(f"line {index}" for index in range(30)),
            ),
        ),
    )
    worker = threading.Thread(target=lambda: result.append(interactor.review(request)))
    worker.start()
    deadline = time.monotonic() + 1
    while interactor.active_request is None and time.monotonic() < deadline:
        time.sleep(0.005)

    assert interactor.submit("d") is True
    assert interactor.details_expanded is True
    assert worker.is_alive()
    assert interactor.scroll_details(5) is True
    assert interactor.detail_scroll == 5
    assert interactor.submit("n") is True
    worker.join(timeout=1)
    assert result[0].approved is False


def test_interaction_parser_uses_kiss_defaults() -> None:
    assert _interaction_response(ConfirmRequest("Confirm", "Proceed?"), "").confirmed
    choice = ChooseOneRequest(
        "Choose", [ChoiceItem("a", "A"), ChoiceItem("b", "B")]
    )
    assert _interaction_response(choice, "2").selected_id == "b"
    text = InputTextRequest("Name", "Value", initial_value="default")
    assert _interaction_response(text, "").value == "default"


def test_session_switch_replaces_execution_projection() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    plan = SimpleNamespace(
        revision=1,
        session_generation=2,
        items=(
            SimpleNamespace(
                step="new session", active_form="restoring", status="in_progress"
            ),
        ),
        explanation=None,
    )
    progress = SimpleNamespace(
        revision=1,
        phase="implementing",
        summary="restored",
        next=None,
    )
    adapter.restore_control_state(plan, progress, session_id="s2")
    assert adapter.execution.state.plan_revision == 1
    assert adapter.execution.state.progress_summary == "restored"
    assert adapter.execution.state.active_plan_item.step == "new session"


def test_restored_conversation_replays_human_rows_only() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [
            {"role": "user", "content": "continue work"},
            {"role": "assistant", "content": "working"},
            {"role": "tool", "content": "internal"},
        ]
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())
    assert "continue work" in rendered
    assert "working" in rendered
    assert "internal" not in rendered
