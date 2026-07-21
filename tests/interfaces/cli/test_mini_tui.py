# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportAssignmentType=false, reportOptionalMemberAccess=false
# Duck-typed stubs (SimpleNamespace, object.__new__) are intentional here.
import threading
import time
from collections import deque
from types import SimpleNamespace
from dataclasses import replace
from prompt_toolkit.utils import get_cwidth

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.app.commands.specs import DuringTurnPolicy
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


def test_yes_no_key_bindings_only_capture_binary_interactions() -> None:
    app = object.__new__(MiniTUIApplication)
    app.interactor = SimpleNamespace(active_request=None)

    bindings = {
        binding.keys: binding
        for binding in app._key_bindings().bindings
        if binding.keys in {("y",), ("n",)}
    }

    assert bindings[("y",)].filter() is False
    assert bindings[("n",)].filter() is False

    app.interactor.active_request = InputTextRequest(
        title="Name",
        prompt="Enter a name",
    )

    assert bindings[("y",)].filter() is False
    assert bindings[("n",)].filter() is False

    app.interactor.active_request = ChooseOneRequest(
        title="Choose",
        items=[ChoiceItem("one", "One")],
    )

    assert bindings[("y",)].filter() is False
    assert bindings[("n",)].filter() is False

    app.interactor.active_request = ConfirmRequest(title="Confirm", message="Continue?")

    assert bindings[("y",)].filter() is True
    assert bindings[("n",)].filter() is True

    app.interactor.active_request = ReviewRequest(title="Review", summary="Command")

    assert bindings[("y",)].filter() is True
    assert bindings[("n",)].filter() is True


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
    assert "steer next: do this instead" in rendered
    assert "steer next: and also that" in rendered
    assert "Ctrl+C cancels the turn and discards queued steers" in rendered
    assert app._interaction_height() == 3


def test_active_turn_plain_text_is_queued_as_model_steering() -> None:
    queued = []
    app = object.__new__(MiniTUIApplication)
    app.agent = SimpleNamespace(submit_user_steering=queued.append)

    app._submit_during_turn("change direction")

    assert queued == ["change direction"]


def test_active_turn_immediate_slash_command_executes_locally(monkeypatch) -> None:
    commands = []
    steering = []
    appended = []
    app = object.__new__(MiniTUIApplication)
    app.agent = SimpleNamespace(submit_user_steering=steering.append)
    app.events = SimpleNamespace(append_user_command=appended.append)
    app.ui_bus = SimpleNamespace(warning=lambda *args, **kwargs: None)
    app.ui_profile = object()
    app.action_registry = object()
    app.current_session_id = "s1"
    app._handle_concurrent_command = commands.append

    monkeypatch.setattr(
        mini_tui_module,
        "parse_command",
        lambda *args, **kwargs: SimpleNamespace(
            action=SimpleNamespace(during_turn=DuringTurnPolicy.IMMEDIATE)
        ),
    )

    class ImmediateThread:
        def __init__(self, *, target, args, **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(mini_tui_module.threading, "Thread", ImmediateThread)

    app._submit_during_turn("/tokens")

    assert appended == ["/tokens"]
    assert commands == ["/tokens"]
    assert steering == []


def test_active_turn_idle_only_slash_command_is_queued_locally(monkeypatch) -> None:
    notices = []
    steering = []
    appended = []
    app = object.__new__(MiniTUIApplication)
    app.agent = SimpleNamespace(submit_user_steering=steering.append)
    app.events = SimpleNamespace(append_user_command=appended.append)
    app.ui_bus = SimpleNamespace(
        info=lambda message, **kwargs: notices.append(message)
    )
    app.ui_profile = object()
    app.action_registry = object()
    app.current_session_id = "s1"
    app._deferred_commands = deque()
    app._deferred_commands_lock = threading.Lock()

    monkeypatch.setattr(
        mini_tui_module,
        "parse_command",
        lambda *args, **kwargs: SimpleNamespace(
            action=SimpleNamespace(
                during_turn=DuringTurnPolicy.DEFER_UNTIL_IDLE
            )
        ),
    )

    app._submit_during_turn("/reset")

    assert appended == ["/reset"]
    assert steering == []
    assert tuple(app._deferred_commands) == ("/reset",)
    assert notices and "Ctrl+C to interrupt and apply it sooner" in notices[0]


def test_queued_command_preview_explains_default_and_accelerated_timing() -> None:
    app = object.__new__(MiniTUIApplication)
    app.interactor = SimpleNamespace(active_request=None)
    app.exit_confirm = False
    app.cancelling = False
    app.running = True
    app.agent = SimpleNamespace(pending_user_steering=lambda: ())
    app._deferred_commands = deque(["/model fast"])
    app._deferred_commands_lock = threading.Lock()

    rendered = "".join(text for _style, text in app._interaction_text())

    assert "when idle: /model fast" in rendered
    assert "Ctrl+C cancels the turn and runs queued commands next" in rendered
    assert app._interaction_height() == 2


def test_next_deferred_command_starts_after_worker_becomes_idle(monkeypatch) -> None:
    applied = []
    cleared = []
    notices = []
    app = object.__new__(MiniTUIApplication)
    app._closed = False
    app._deferred_commands = deque(["/model fast"])
    app._deferred_commands_lock = threading.Lock()
    app.agent = SimpleNamespace(clear_stop_request=lambda: cleared.append(True))
    app.ui_bus = SimpleNamespace(info=lambda message, **kwargs: notices.append(message))
    app._handle_input = lambda *args: applied.append(args)

    class ImmediateThread:
        def __init__(self, *, target, args, **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(mini_tui_module.threading, "Thread", ImmediateThread)

    assert app._start_next_deferred_command() is True

    assert applied == [("/model fast", False)]
    assert cleared == [True]
    assert notices == ["Applying queued command now: /model fast"]
    assert app.running is True
    assert tuple(app._deferred_commands) == ()


def test_completed_agent_turn_starts_deferred_command_before_marking_idle(
    monkeypatch,
) -> None:
    started = []
    chats = []
    app = object.__new__(MiniTUIApplication)
    app.agent = SimpleNamespace(
        session_generation=1,
        current_session_id="s1",
        chat=chats.append,
    )
    app.config = SimpleNamespace()
    app.current_session_id = "s1"
    app.session_exit_time = None
    app.ui_bus = SimpleNamespace()
    app.ui_profile = object()
    app.action_registry = object()
    app.sessions_dir = None
    app.skills_service = None
    app.cancelling = True
    app.running = True
    app.invalidate = lambda: None
    app._start_next_deferred_command = lambda: started.append(True) or True

    monkeypatch.setattr(
        mini_tui_module,
        "handle_command",
        lambda *args, **kwargs: {
            "action": "chat",
            "action_id": None,
            "session_id": "s1",
            "session_exit_time": None,
        },
    )

    app._handle_input("finish this")

    assert chats == ["finish this"]
    assert started == [True]
    assert app.cancelling is False
    assert app.running is True


def test_exit_finalizer_skips_duplicate_save_after_exit_command() -> None:
    prepared = []
    app = object.__new__(MiniTUIApplication)
    app._exit_session_saved = True
    app.agent = SimpleNamespace(messages=[{"role": "user", "content": "done"}])
    app.config = SimpleNamespace(session_auto_save=True)
    app._prepare_forced_exit = prepared.append

    app._save_exit_session()

    assert prepared == ["CLI session closed"]


def test_wrapped_row_count_grows_input_height_with_cjk_awareness() -> None:
    assert _wrapped_row_count("", 40) == 1
    assert _wrapped_row_count("short", 40) == 1
    assert _wrapped_row_count("a" * 41, 40) == 2
    # CJK characters occupy two cells each.
    assert _wrapped_row_count("汉" * 21, 40) == 2
    assert _wrapped_row_count("a" * 400, 40) == 8


def test_command_popup_adopts_candidate_and_hides_on_non_slash() -> None:
    from reuleauxcoder.interfaces.cli.command_popup import PopupEntry

    app = object.__new__(MiniTUIApplication)
    app.interactor = SimpleNamespace(active_request=None)
    app._selection = None
    app.input_buffer = SimpleNamespace(text="/mo", cursor_position=3)
    app._popup_entries = (
        PopupEntry("/mode", "Choose the active session mode", False, False),
        PopupEntry("/model", "Choose model profiles and routing", False, False),
    )
    app._popup_index = 0
    app._popup_last_text = ""
    app._popup_dismissed = False
    app.invalidate = lambda: None

    candidates = app._popup_candidates()
    assert [entry.completion for entry in candidates] == ["/mode", "/model"]
    assert app._popup_height() == 2

    # Adopting fills the buffer without submitting.
    app._popup_adopt()
    assert app.input_buffer.text == "/mode"

    # Non-slash input hides the popup.
    app.input_buffer.text = "hello"
    assert app._popup_candidates() == ()
    assert app._popup_height() == 0


def test_command_popup_dismissed_until_text_changes() -> None:
    from reuleauxcoder.interfaces.cli.command_popup import PopupEntry

    app = object.__new__(MiniTUIApplication)
    app.interactor = SimpleNamespace(active_request=None)
    app._selection = None
    app.input_buffer = SimpleNamespace(text="/he", cursor_position=3)
    app._popup_entries = (
        PopupEntry("/help", "Show command help", False, False),
    )
    app._popup_index = 0
    app._popup_last_text = ""
    app._popup_dismissed = False
    app.invalidate = lambda: None

    assert app._popup_candidates()
    app._popup_dismissed = True
    assert app._popup_candidates() == ()

    # Typing more reopens the popup.
    app.input_buffer.text = "/hel"
    assert app._popup_candidates()


def test_mode_view_opens_selection_panel_and_confirm_resubmits() -> None:
    from types import SimpleNamespace as NS

    from reuleauxcoder.app.commands.view_models import (
        ModeProfileViewModel,
        ModesViewModel,
    )

    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app.invalidate = lambda: None
    accepted = []
    app.input_buffer = NS(text="", cursor_position=0)
    app._accept_buffer = lambda buffer: accepted.append(buffer.text)

    payload = NS(
        view_type="mode_profiles",
        title="Modes",
        action="open",
        focus=True,
        view_model=ModesViewModel(
            active_mode="coder",
            modes=(
                ModeProfileViewModel(
                    name="coder",
                    active=True,
                    description="Default coding mode",
                    tools=(),
                    prompt_append="",
                    allowed_subagent_modes=(),
                ),
                ModeProfileViewModel(
                    name="plan",
                    active=False,
                    description="Planning first",
                    tools=(),
                    prompt_append="",
                    allowed_subagent_modes=(),
                ),
            ),
            diagnostics=(),
        ),
    )

    assert app._open_interactive_view(payload) is True
    assert app._selection is not None
    assert app._selection.selected.label == "coder"

    app._selection.move(1)
    app._selection_confirm()
    assert accepted == ["/mode switch plan"]
    assert app._selection is None


def test_unknown_view_type_is_not_claimed() -> None:
    from types import SimpleNamespace as NS

    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app.invalidate = lambda: None

    payload = NS(
        view_type="token_usage", title="Tokens", action="open", focus=True, view_model=NS()
    )
    assert app._open_interactive_view(payload) is False
    assert app._selection is None


def _model_view_payload() -> object:
    from types import SimpleNamespace as NS

    from reuleauxcoder.app.commands.view_models import (
        ModelListViewModel,
        ModelProfileViewModel,
    )

    return NS(
        view_type="model_profiles",
        title="Models",
        action="open",
        focus=True,
        view_model=ModelListViewModel(
            active_main="sonnet",
            active_sub="haiku",
            current_model="claude-sonnet",
            profiles=(
                ModelProfileViewModel(
                    name="haiku",
                    model="claude-haiku",
                    active_main=False,
                    active_sub=True,
                    base_url=None,
                    max_tokens=4096,
                    temperature=0.5,
                    max_context_tokens=200000,
                    api_key_hint="...key",
                ),
                ModelProfileViewModel(
                    name="sonnet",
                    model="claude-sonnet",
                    active_main=True,
                    active_sub=False,
                    base_url=None,
                    max_tokens=8192,
                    temperature=1.0,
                    max_context_tokens=200000,
                    api_key_hint="...key",
                ),
            ),
            diagnostics=(),
        ),
    )


def test_model_view_opens_slot_panel_then_profile_panel() -> None:
    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app._selection_stack = []
    app._model_slot_profiles = {}
    app.invalidate = lambda: None
    accepted = []
    app.input_buffer = SimpleNamespace(text="", cursor_position=0)
    app._accept_buffer = lambda buffer: accepted.append(buffer.text)

    assert app._open_interactive_view(_model_view_payload()) is True
    assert app._selection.view_type == "model_slots"
    assert len(app._selection.items) == 4

    # Confirm "Session · Main model" opens the profile panel.
    app._selection_confirm()
    assert app._selection.view_type == "model_profiles"
    assert len(app._selection_stack) == 1
    assert app._selection.selected.label == "sonnet"  # current main preselected

    # Confirm a profile resubmits the canonical command.
    app._selection_confirm()
    assert accepted == ["/model use-main sonnet"]
    assert app._selection is None
    assert app._selection_stack == []


def test_model_panel_escape_returns_to_slot_panel() -> None:
    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app._selection_stack = []
    app._model_slot_profiles = {}
    app.invalidate = lambda: None

    app._open_interactive_view(_model_view_payload())
    app._selection.move(1)  # Session · Sub-agent model
    app._selection_confirm()
    assert app._selection.view_type == "model_profiles"
    assert app._selection.selected.label == "haiku"  # current sub preselected

    app._selection_close()  # back to slots
    assert app._selection.view_type == "model_slots"
    app._selection_close()  # close entirely
    assert app._selection is None


def _approval_view_payload() -> object:
    from types import SimpleNamespace as NS

    from reuleauxcoder.app.runtime.approval import ApprovalRuleView, ApprovalView

    return NS(
        view_type="approval_rules",
        title="Approval Rules",
        action="open",
        focus=True,
        view_model=ApprovalView(
            default_mode="warn",
            rules=[
                ApprovalRuleView(
                    scope="tool",
                    action="require_approval",
                    tool_name="write_file",
                    source="session",
                ),
                ApprovalRuleView(
                    scope="mcp_server",
                    action="allow",
                    tool_source="mcp",
                    mcp_server="github",
                    source="global",
                ),
            ],
        ),
    )


def test_approval_view_opens_targets_then_actions() -> None:
    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app._selection_stack = []
    app._approval_targets = {}
    app.invalidate = lambda: None
    accepted = []
    app.input_buffer = SimpleNamespace(text="", cursor_position=0)
    app._accept_buffer = lambda buffer: accepted.append(buffer.text)

    assert app._open_interactive_view(_approval_view_payload()) is True
    assert app._selection.view_type == "approval_rules"
    labels = [item.label for item in app._selection.items]
    assert labels == ["tool:write_file", "mcp:github"]

    # Confirm session-scoped target -> action picker with set (not set-global).
    app._selection_confirm()
    assert app._selection.view_type == "approval_actions"
    assert app._selection.selected.label == "require_approval"  # current preselected

    app._selection.move(1)  # deny
    app._selection_confirm()
    assert accepted == ["/approval set tool:write_file deny"]
    assert app._selection is None


def test_approval_global_target_uses_set_global() -> None:
    app = object.__new__(MiniTUIApplication)
    app._selection = None
    app._selection_stack = []
    app._approval_targets = {}
    app.invalidate = lambda: None
    accepted = []
    app.input_buffer = SimpleNamespace(text="", cursor_position=0)
    app._accept_buffer = lambda buffer: accepted.append(buffer.text)

    app._open_interactive_view(_approval_view_payload())
    app._selection.move(1)  # mcp:github (global source)
    app._selection_confirm()
    assert app._selection.selected.label == "allow"

    app._selection_confirm()
    assert accepted == ["/approval set-global mcp:github allow"]


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
