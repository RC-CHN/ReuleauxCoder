import threading
import time
from types import SimpleNamespace

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.approval import ApprovalSection, ApprovalSectionKind
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    AssistantContentDelta,
    RuntimeEvent,
    SubagentJobChanged,
    agent_event_to_runtime_event,
)
from reuleauxcoder.interfaces.cli.mini_tui import (
    MiniTUIEventAdapter,
    MiniTUIInteractor,
    MiniTUIApplication,
    _interaction_lines,
    _interaction_response,
    _wrap_fragments,
)
import reuleauxcoder.interfaces.cli.mini_tui as mini_tui_module
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventBus,
    UIEventKind,
)
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


def test_static_transcript_cells_reuse_width_revision_fragment_cache(
    monkeypatch,
) -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [{"role": "assistant", "content": "**stable**"}]
    )
    original = mini_tui_module._cell_fragments
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mini_tui_module, "_cell_fragments", counted)
    adapter.transcript_fragments()
    adapter.transcript_fragments()
    assert calls == 1

    adapter.set_viewport_width(70)
    adapter.transcript_fragments()
    assert calls == 2


def test_large_static_transcript_reuses_aggregate_layout() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [
            {
                "role": "assistant" if index % 2 else "user",
                "content": f"row {index} · 中文 🚀 **markdown**",
            }
            for index in range(500)
        ]
    )

    first = adapter.transcript_fragments()
    second = adapter.transcript_fragments()
    assert second is first

    adapter.set_viewport_width(64)
    resized = adapter.transcript_fragments()
    assert resized is not first


def test_visual_prewrap_counts_cjk_and_emoji_width() -> None:
    wrapped = _wrap_fragments(
        [("class:assistant", "ab中文🚀cd")],
        width=6,
    )
    text = "".join(fragment for _style, fragment in wrapped)

    assert text == "ab中文\n🚀cd"


def test_child_internals_stay_out_of_transcript_but_approval_remains_visible() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")

    child_delta = RuntimeEvent(
        payload=AssistantContentDelta("private child reasoning"),
        agent_id="child-1",
    )
    child_job = RuntimeEvent(
        payload=SubagentJobChanged(
            job_id="sj-1",
            mode="explore",
            task="inspect routing",
            status="running",
        ),
        agent_id="child-1",
    )
    child_approval = RuntimeEvent(
        payload=ApprovalRequested(
            request_id="approval-child",
            title="Approve child edit",
        ),
        agent_id="child-1",
    )

    for runtime in (child_delta, child_job, child_approval):
        adapter.on_ui_event(
            UIEvent.info(
                runtime.kind.value,
                kind=UIEventKind.AGENT,
                payload=RuntimeEventPayload(runtime),
            )
        )

    rendered = "".join(text for _style, text in adapter.transcript_fragments())
    panel = "\n".join(adapter.panel_lines(100))

    assert "private child reasoning" not in rendered
    assert "inspect routing" not in rendered
    assert "APPROVE CHILD EDIT" in rendered
    assert "inspect routing" in panel


def test_event_projection_failure_is_isolated_to_ui_diagnostic() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.execution.apply = lambda _event: (_ for _ in ()).throw(
        ValueError("bad projection")
    )
    runtime = agent_event_to_runtime_event(
        AgentEvent.chat_start("keep running"), agent_id="main"
    )
    adapter.on_ui_event(
        UIEvent.info(
            "turn_started",
            kind=UIEventKind.AGENT,
            payload=RuntimeEventPayload(runtime),
        )
    )

    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert "UI projection skipped: bad projection" in rendered


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


def test_unknown_review_input_does_not_resolve_request() -> None:
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
    assert worker.is_alive()
    assert interactor.submit("n") is True
    worker.join(timeout=1)
    assert result[0].approved is False


def test_review_diff_is_projected_into_main_transcript_and_bottom_is_compact() -> None:
    adapter = MiniTUIEventAdapter()
    request = ReviewRequest(
        "Approval required: edit_file",
        "Review this edit.",
        sections=(
            ApprovalSection(
                "diff",
                "Proposed edit diff",
                ApprovalSectionKind.DIFF,
                "--- a/demo.py\n+++ b/demo.py\n-old\n+new",
            ),
        ),
        request_id="approval-main",
    )
    adapter.on_ui_event(
        UIEvent.info(
            request.title,
            kind=UIEventKind.APPROVAL,
            payload=InteractionPromptPayload(request),
        )
    )

    rendered = "".join(text for _style, text in adapter.transcript_fragments())
    controls = _interaction_lines(request)

    assert "PROPOSED EDIT DIFF" in rendered
    assert "+new" in rendered
    assert controls == ["[Enter/Y] Approve   [N] Reject"]
    assert "+new" not in "\n".join(controls)


def test_interaction_parser_uses_kiss_defaults() -> None:
    assert _interaction_response(ConfirmRequest("Confirm", "Proceed?"), "").confirmed
    choice = ChooseOneRequest("Choose", [ChoiceItem("a", "A"), ChoiceItem("b", "B")])
    assert _interaction_response(choice, "2").selected_id == "b"
    text = InputTextRequest("Name", "Value", initial_value="default")
    assert _interaction_response(text, "").value == "default"


def test_transcript_scroll_reenables_tail_follow_at_bottom() -> None:
    app = object.__new__(MiniTUIApplication)
    app._transcript_max_scroll = 30
    app._follow_transcript = True
    app.transcript_pane = SimpleNamespace(vertical_scroll=30)
    app.invalidate = lambda: None

    app._scroll_transcript(-10)
    assert app.transcript_pane.vertical_scroll == 20
    assert app._follow_transcript is False

    app._scroll_transcript(10)
    assert app.transcript_pane.vertical_scroll == 30
    assert app._follow_transcript is True


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
