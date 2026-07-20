import threading
import time
from types import SimpleNamespace
from dataclasses import replace
from prompt_toolkit.utils import get_cwidth

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
    ALTERNATE_SCROLL_DISABLE,
    ALTERNATE_SCROLL_ENABLE,
    MINI_TUI_MOUSE_SUPPORT,
    MiniTUIEventAdapter,
    MiniTUIInteractor,
    MiniTUIApplication,
    _execution_panel_rows,
    _interaction_lines,
    _interaction_response,
    _wrap_fragments,
    _wrapped_row_count,
)
from reuleauxcoder.interfaces.cli.virtual_transcript import (
    VirtualTranscriptControl,
    VirtualTranscriptLayout,
    VisualCell,
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


def test_mini_tui_leaves_mouse_to_terminal_native_selection() -> None:
    assert MINI_TUI_MOUSE_SUPPORT is False


def test_tool_cell_leads_with_name_and_right_aligns_status() -> None:
    from types import SimpleNamespace

    from reuleauxcoder.presentation.models import ToolCell, ToolCellStatus
    from reuleauxcoder.interfaces.cli.mini_tui import (
        _cell_fragments,
        _fragments_to_visual_lines,
    )

    outcome = SimpleNamespace(
        summary="npm run build",
        ui_text=lambda include_details=True: "",
    )
    cell = ToolCell(
        id="t1",
        tool_call_id="tc1",
        name="shell",
        arguments={"command": "npm run build"},
        status=ToolCellStatus.SUCCEEDED,
        outcome=outcome,
        output="✓ built in 4.21s",
    )

    lines = _fragments_to_visual_lines(
        _wrap_fragments(_cell_fragments(cell, width=50), width=50)
    )
    header = "".join(text for _style, text in lines[0])

    assert header.startswith(" shell · npm run build")
    assert header.rstrip().endswith("SUCCEEDED")
    assert get_cwidth(header) <= 50


def test_interaction_lane_shows_queued_steering_while_running() -> None:
    app = object.__new__(MiniTUIApplication)
    app.interactor = SimpleNamespace(active_request=None)
    app.exit_confirm = False
    app.cancelling = False
    app.running = True
    app.agent = SimpleNamespace(
        pending_user_steering=lambda: ("do this instead", "and also that"),
    )

    rendered = "".join(text for _style, text in app._interaction_text())
    assert " ↳ do this instead" in rendered
    assert " ↳ and also that" in rendered
    assert "Agent running" in rendered
    assert app._interaction_height() == 3


def test_wrapped_row_count_grows_input_height_with_cjk_awareness() -> None:
    assert _wrapped_row_count("", 40) == 1
    assert _wrapped_row_count("short", 40) == 1
    assert _wrapped_row_count("a" * 41, 40) == 2
    # CJK characters occupy two cells each.
    assert _wrapped_row_count("汉" * 21, 40) == 2
    assert _wrapped_row_count("a" * 400, 40) == 8


def test_structured_panel_is_fixed_height_until_details_are_expanded() -> None:
    adapter = MiniTUIEventAdapter()
    view = adapter.panel_view(now=100.0)

    wide = _execution_panel_rows(view, width=100, expanded=False)
    narrow = _execution_panel_rows(view, width=40, expanded=False)
    expanded = _execution_panel_rows(
        view,
        width=100,
        expanded=True,
        details=("MODEL demo", "ROOT /workspace", "SESSION new"),
    )

    assert len(wide) == 4
    assert len(narrow) == 3
    assert len(expanded) == 7
    assert "RUN" in "".join(text for _style, text in wide[0])
    assert "MODEL" in "".join(text for row in expanded for _style, text in row)


def test_alternate_scroll_protocol_keeps_native_selection_and_wheel_keys() -> None:
    writes = []
    output = SimpleNamespace(
        write_raw=writes.append,
        flush=lambda: writes.append("flush"),
    )
    app = object.__new__(MiniTUIApplication)
    app.application = SimpleNamespace(output=output)

    app._set_alternate_scroll(enabled=True)
    app._set_alternate_scroll(enabled=False)

    assert writes == [
        ALTERNATE_SCROLL_ENABLE,
        "flush",
        ALTERNATE_SCROLL_DISABLE,
        "flush",
    ]


def test_alternate_scroll_routes_approval_wheel_to_transcript() -> None:
    app = object.__new__(MiniTUIApplication)
    app.input_buffer = SimpleNamespace(text="")
    app.interactor = SimpleNamespace(active_request=None)

    assert app._should_route_arrows_to_transcript() is True
    app.input_buffer.text = "editing"
    assert app._should_route_arrows_to_transcript() is False
    app.interactor.active_request = object()
    assert app._should_route_arrows_to_transcript() is True


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


def test_thousand_cell_transcript_reuses_virtual_layout() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [
            {
                "role": "assistant" if index % 2 else "user",
                "content": f"row {index} · 中文 🚀 **markdown**",
            }
            for index in range(1_000)
        ]
    )

    first = adapter.transcript_layout(100)
    second = adapter.transcript_layout(100)
    assert len(adapter.transcript.state.transcript.cells) == 1_000
    assert second is first

    resized = adapter.transcript_layout(64)
    assert resized is not first


def test_unchanged_transcript_layout_uses_model_revision_fast_path(
    monkeypatch,
) -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation([{"role": "assistant", "content": "stable"}])
    first = adapter.transcript_layout(80)

    def unexpected_compose(_cells):
        raise AssertionError("unchanged transcript should not be recomposed")

    monkeypatch.setattr(mini_tui_module, "compose_transcript", unexpected_compose)

    assert adapter.transcript_layout(80) is first


def test_transcript_groups_turns_with_role_labels_and_one_separator() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "before tool"},
            {"role": "assistant", "content": "same turn continuation"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]
    )

    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert rendered.count(" FORGE ") == 2
    assert rendered.count("╶────────────────") == 1
    assert rendered.count(" YOU ") == 2


def test_visual_prewrap_counts_cjk_and_emoji_width() -> None:
    wrapped = _wrap_fragments(
        [("class:assistant", "ab中文🚀cd")],
        width=6,
    )
    text = "".join(fragment for _style, fragment in wrapped)

    assert text == "ab中文\n🚀cd"


def test_stream_revision_reformats_only_changed_cell(monkeypatch) -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [{"role": "assistant", "content": f"stable {index}"} for index in range(100)]
    )
    adapter.transcript_layout(80)
    original = mini_tui_module._cell_fragments
    calls = []

    def counted(cell, **kwargs):
        calls.append(cell.id)
        return original(cell, **kwargs)

    monkeypatch.setattr(mini_tui_module, "_cell_fragments", counted)
    last = adapter.transcript.state.transcript.cells[-1]
    adapter.transcript.state.transcript.replace(
        replace(last, text=last.text + " next chunk", revision=last.revision + 1)
    )

    adapter.transcript_layout(80)
    assert calls == [last.id]


def test_virtual_control_resolves_only_requested_rows() -> None:
    calls = []

    class CountingLayout(VirtualTranscriptLayout):
        def get_line(self, line_number):
            calls.append(line_number)
            return super().get_line(line_number)

    layout = CountingLayout(
        tuple(
            VisualCell(
                key=(f"cell-{index}", 0, 80, 0),
                lines=((("", f"row {index}"),),),
            )
            for index in range(1_000)
        )
    )
    control = VirtualTranscriptControl(
        lambda _width: layout,
        lambda: mini_tui_module.Point(x=0, y=500),
    )
    content = control.create_content(80, 20)

    assert content.line_count == 1_000
    assert calls == []
    assert content.get_line(500) == [("", "row 500")]
    assert calls == [500]


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
    app._transcript_scroll = 30
    app._follow_transcript = True
    app.transcript_pane = SimpleNamespace(vertical_scroll=30)
    app.invalidate = lambda: None

    app._scroll_transcript(-10)
    assert app._transcript_scroll == 20
    assert app.transcript_pane.vertical_scroll == 20
    assert app._follow_transcript is False

    app._scroll_transcript(10)
    assert app._transcript_scroll == 30
    assert app.transcript_pane.vertical_scroll == 30
    assert app._follow_transcript is True


def test_before_render_keeps_scrolled_view_stable_and_tail_sticky() -> None:
    app = object.__new__(MiniTUIApplication)
    line_count = [100]
    app.events = SimpleNamespace(
        transcript_layout_rebased=lambda _width, scroll: (
            SimpleNamespace(line_count=line_count[0]),
            scroll,
        )
    )
    app.application = SimpleNamespace(
        output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80, rows=40))
    )
    app.transcript_control = SimpleNamespace(last_height=30)
    app.transcript_pane = SimpleNamespace(vertical_scroll=0)
    app._panel_height = lambda: 3
    app._interaction_height = lambda: 2
    app._last_terminal_rows = 40
    app._transcript_scroll = 0
    app._follow_transcript = True

    app._before_render(None)
    assert app.transcript_pane.vertical_scroll == 70

    app._follow_transcript = False
    app._transcript_scroll = 25
    line_count[0] = 130
    app._before_render(None)
    assert app.transcript_pane.vertical_scroll == 25
    assert app._follow_transcript is False

    app._transcript_scroll = app._transcript_max_scroll
    app.invalidate = lambda: None
    app._scroll_transcript(0)
    line_count[0] = 135
    app._before_render(None)
    assert app.transcript_pane.vertical_scroll == 105
    assert app._follow_transcript is True


def test_virtual_layout_rebases_scroll_to_same_cell_after_markdown_reflow() -> None:
    old = VirtualTranscriptLayout(
        (
            VisualCell(
                key=("markdown", 1, 80, 0),
                lines=((("", "a"),), (("", "b"),), (("", "c"),)),
            ),
            VisualCell(
                key=("answer", 0, 80, 0),
                lines=((("", "one"),), (("", "two"),), (("", "three"),)),
            ),
        )
    )
    anchor = old.anchor_at(4)
    assert anchor == ("answer", 1)

    reflowed = VirtualTranscriptLayout(
        (
            VisualCell(
                key=("markdown", 2, 80, 0),
                lines=tuple((("", f"line-{index}"),) for index in range(8)),
            ),
            VisualCell(
                key=("answer", 0, 80, 0),
                lines=((("", "one"),), (("", "two"),), (("", "three"),)),
            ),
        )
    )

    assert reflowed.line_for_anchor(anchor) == 9
    assert reflowed.anchor_at(9) == ("answer", 1)


def test_mixed_transcript_repeated_resize_never_reuses_old_width_rows() -> None:
    adapter = MiniTUIEventAdapter()
    transcript = adapter.transcript.state.transcript
    transcript.append(
        mini_tui_module.AssistantCell(
            id="mixed:markdown",
            text=(
                "**粗体中文 🚀**\n\n| 项目 | 状态 |\n| --- | --- |\n| parser | ready |"
            ),
            complete=True,
        )
    )
    transcript.append(
        mini_tui_module.DiffCell(
            id="mixed:diff",
            path="demo.py",
            diff="--- a/demo.py\n+++ b/demo.py\n-old 中文\n+new 🚀",
        )
    )

    layouts = [adapter.transcript_layout(width) for width in (72, 31, 90, 31)]

    assert all(
        before.lines is after.lines
        for before, after in zip(layouts[1].cells, layouts[3].cells, strict=True)
    )
    assert layouts[0] is not layouts[1]
    for width, layout in zip((72, 31, 90, 31), layouts, strict=True):
        assert all(cell.key[2] == width for cell in layout.cells)
        for line_number in range(layout.line_count):
            text = "".join(
                fragment for _style, fragment in layout.get_line(line_number)
            )
            assert get_cwidth(text) <= width
    narrow = "\n".join(
        "".join(text for _style, text in layouts[1].get_line(line))
        for line in range(layouts[1].line_count)
    )
    assert "**" not in narrow
    assert "| 项目 |" not in narrow


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


def test_new_session_clear_removes_visible_transcript_and_render_cache() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.append_restored_conversation(
        [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]
    )
    adapter.transcript_layout(80)

    assert adapter.transcript.state.transcript.cells
    assert adapter._cell_visual_cache

    adapter.clear_transcript()

    assert adapter.transcript.state.transcript.cells == ()
    assert adapter._cell_visual_cache == {}
    assert adapter._transcript_layout_key == ()
    assert adapter.transcript_layout(80).cells == ()


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
