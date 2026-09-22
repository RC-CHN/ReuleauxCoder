import threading
from types import MappingProxyType
import pytest

from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.interaction_contracts import ConfirmRequest, InputTextRequest
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.ui_events import UIEvent
from reuleauxcoder.extensions.command.builtin.thinking import SetEffortCommand
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def test_initialization_describes_backend_without_exposing_local_history(runtime):
    info = runtime.client.info
    assert (
        info["presentation"]["reasoning_display"] == runtime.config.ui.reasoning_display
    )
    assert info["model_configured"] is True
    assert info["host_mode"] is False
    assert info["runtime_environment"]["system"]
    assert "history_file" not in info
    assert "api_key" not in info
    info["presentation"]["reasoning_display"] = "inline"
    assert runtime.config.ui.reasoning_display != "inline"


def test_peer_controls_and_final_response_cross_the_runtime(runtime):
    entered, release = threading.Event(), threading.Event()
    completed = []
    runtime.loop.run = lambda: (entered.set(), release.wait(3), "done")[-1]
    runtime.client.on_completed = completed.append
    assert runtime.client.admit_steering("too early") is None
    runtime.client.submit("hello")
    assert entered.wait(2)
    try:
        steering_id = runtime.client.admit_steering("new direction")
        assert steering_id
        assert runtime.client.interrupt()["outcome"] == "promoted"
        runtime.client.stop()
        assert runtime.agent.stop_requested()
    finally:
        release.set()
    runtime.client.wait_idle()
    assert completed[-1].response == "done"


def test_snapshot_revision_tracks_changes_and_conditional_reads(runtime):
    client = runtime.client
    changes = []
    client.on_state = changes.append
    original = client.state
    for _ in range(3):
        client.refresh()
        assert runtime.server.snapshot().revision == original.revision
        assert (
            client.peer.request(
                "runtime.snapshot", {"known_revision": original.revision}
            )
            is None
        )
    assert changes == []
    runtime.agent.llm.model = "changed-model"
    client.refresh()
    assert client.state.model == "changed-model"
    assert client.state.revision == original.revision + 1
    assert changes == [client.state]
    assert decode(client.peer.request("runtime.snapshot")) == client.state
    client._state(original)
    assert client.state.model == "changed-model"


def test_history_rpc_uses_the_same_saved_records_and_generation(runtime, tmp_path):
    from reuleauxcoder.infrastructure.persistence.session_store import SessionStore

    ledger = runtime.agent.history_ledger
    event = ledger.append_message(
        {"role": "user", "content": "Find 原始内容"}, source="user", turn_id="turn_1"
    )
    SessionStore(tmp_path).save(
        session_id="test-session",
        messages=[],
        model="test",
        history_events=list(ledger.events),
    )
    reply = decode(
        runtime.client.peer.request("history.search", {"pattern": "原始内容"})
    )
    assert reply["session_generation"] == runtime.agent.session_generation
    found = reply["page"].records[0]
    assert found.event_id == event.event_id
    page = decode(
        runtime.client.peer.request("history.read", {"event_id": found.event_id})
    )["page"]
    assert page.records[0].content == "Find 原始内容"
    assert page.records[0].turn_id == "turn_1"


def test_workspace_git_and_effective_modes_cross_rpc(runtime, tmp_path):
    import subprocess

    from reuleauxcoder.infrastructure.version_control import GitMonitor

    assert decode(runtime.client.peer.request("runtime.git")) is None
    runtime.agent.git_monitor = GitMonitor(tmp_path)
    snapshot = decode(runtime.client.peer.request("runtime.git"))
    assert not snapshot.available
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "backend-workspace.txt").write_text("from the backend\n")
    snapshot = decode(runtime.client.peer.request("runtime.git"))
    assert snapshot.available
    assert any(file.path == "backend-workspace.txt" for file in snapshot.files)
    runtime.client.refresh()
    assert runtime.client.state.mode == runtime.agent.active_mode
    assert runtime.client.state.approval_policy == runtime.config.approval.default_mode


def test_chat_and_all_builtin_views_use_serialized_events(runtime, caplog):
    runtime.client.submit("hello")
    runtime.client.wait_idle()
    assert runtime.agent.messages[-1]["content"] == "hello"
    for text in (
        "/help",
        "/model",
        "/mode",
        "/approval",
        "/skills",
        "/mcp",
        "/agents",
        "/ps",
        "/thinking",
        "/session",
        "/config",
        "/status perf",
        "/tokens",
    ):
        runtime.client.submit(text)
        runtime.client.wait_idle()
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert any(event.kind.value == "view" for event in runtime.bus.history_snapshot())
    assert runtime.client.state.session_id == "test-session"


def test_panel_action_parameters_round_trip_without_importing_handlers(runtime):
    runtime.client.submit(
        ActionRequest("thinking.set_effort", SetEffortCommand("high"))
    )
    runtime.client.wait_idle()
    assert runtime.agent.llm.reasoning_effort == "high"
    assert decode(
        encode(ActionRequest("thinking.set_effort", SetEffortCommand("low")))
    ).command == {"level": "low"}


def test_backend_queues_commands_and_drains_at_turn_completion(runtime):
    entered, release = threading.Event(), threading.Event()
    runtime.loop.run = lambda: (entered.set(), release.wait(2), "done")[-1]
    runtime.client.submit("chat")
    assert entered.wait(2)
    try:
        admission = runtime.client.submit("/reset")
        assert admission.status == "queued"
        assert runtime.client.state.queued_commands == ("/reset",)
        runtime.client.submit("/help")
    finally:
        release.set()
    runtime.client.wait_idle()
    assert runtime.client.state.queued_commands == ()
    assert runtime.agent.messages == []


def test_bidirectional_interaction_does_not_block_request_receiver(runtime):
    entered, release = threading.Event(), threading.Event()
    requests = []

    def confirm(request):
        requests.append(request)
        entered.set()
        assert release.wait(2)
        from reuleauxcoder.app.interaction_contracts import ConfirmResponse

        return ConfirmResponse(True)

    runtime.interactor.confirm = confirm
    runtime.loop.run = lambda: str(
        runtime.agent.ui_interactor.confirm(ConfirmRequest("Confirm", "Continue?"))
    )
    runtime.client.submit("ask")
    assert entered.wait(2)
    try:
        runtime.client.refresh()
        assert runtime.client.state.running
    finally:
        release.set()
    runtime.client.wait_idle()
    assert requests[0].message == "Continue?"


def test_backend_owns_interrupt_and_stop_state(runtime):
    entered = threading.Event()

    def run():
        entered.set()
        assert runtime.agent._stop_event.wait(2)
        return "stopped"

    runtime.loop.run = run
    runtime.client.submit("work")
    assert entered.wait(2)
    assert runtime.client.interrupt()["outcome"] == "stop_requested"
    runtime.client.wait_idle()
    assert runtime.agent.stop_requested()
    assert not runtime.client.state.running
    assert not runtime.client.state.stopping


@pytest.mark.parametrize("value", ["work", "/model"])
def test_interrupt_before_worker_execution_is_preserved(runtime, monkeypatch, value):
    entered, release = threading.Event(), threading.Event()
    submit = runtime.server.commands.submit

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return submit(*args, **kwargs)

    monkeypatch.setattr(runtime.server.commands, "submit", delayed)
    runtime.client.submit(value)
    assert entered.wait(3)
    try:
        assert runtime.client.interrupt()["outcome"] == "stop_requested"
    finally:
        release.set()
    runtime.client.wait_idle()
    assert runtime.agent.stop_requested()

    monkeypatch.setattr(runtime.server.commands, "submit", submit)
    observed = []
    runtime.loop.run = lambda: observed.append(runtime.agent.stop_requested()) or "done"
    runtime.client.submit("next user turn")
    runtime.client.wait_idle()
    assert observed == [False]


def test_user_metadata_cannot_be_decoded_as_a_contract():
    original = UIEvent.info(
        "data", arbitrary={"$type": "ActionRequest", "fields": {"x": 1}}
    )
    assert decode(encode(original)) == original


def test_tool_outcome_readonly_metadata_round_trips():
    from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome

    outcome = ToolOutcome(
        stdout="complete tool output",
        metadata=MappingProxyType(
            {"nested": (MappingProxyType({"$type": "user-data", "value": 1}),)}
        ),
    )
    assert decode(encode(outcome)) == outcome


def test_executed_tool_result_reaches_frontend(runtime, tmp_path):
    from reuleauxcoder.app.ui_events import RuntimeEventPayload
    from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
    from reuleauxcoder.domain.llm.models import ToolCall
    from reuleauxcoder.domain.runtime.events import ToolCallFinished
    from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool

    (tmp_path / "retained.txt").write_text("test")
    runtime.agent.tools = [ListFileTool()]
    runtime.loop.run = lambda: ToolExecutor(runtime.agent).execute(
        ToolCall(id="list-call", name="list_file", arguments={"path": str(tmp_path)})
    )
    received = []
    delivered = threading.Event()

    def observe(event):
        if isinstance(event.payload, RuntimeEventPayload) and isinstance(
            event.payload.event.payload, ToolCallFinished
        ):
            received.append(event.payload.event.payload)
            delivered.set()

    runtime.bus.subscribe(observe, replay_history=False)
    runtime.client.submit("List files")
    runtime.client.wait_idle()
    assert delivered.wait(2), "ToolCallFinished must cross the JSON-RPC boundary"
    assert received[0].outcome.success
    assert "retained.txt" in received[0].outcome.display_text
    assert received[0].tool_call_id == "list-call"


def test_wire_deadlines_are_rebased_at_the_receiving_process(runtime):
    import time

    deadlines = []
    from reuleauxcoder.app.interaction_contracts import InputTextResponse

    runtime.interactor.input_text = lambda request: (
        deadlines.append(request.deadline),
        InputTextResponse("ok"),
    )[1]
    result = runtime.server.interactions.input_text(
        InputTextRequest("Text", "Value", deadline=time.monotonic() + 10)
    )
    assert result.value == "ok"
    assert 0 < deadlines[0] - time.monotonic() <= 10


def test_operation_failure_preserves_input_and_reaches_caller(runtime, caplog):
    def fail():
        raise RuntimeError("test failure with content")

    runtime.loop.run = fail
    runtime.client.submit("retain this input")
    with pytest.raises(RpcError, match="test failure with content"):
        runtime.client.wait_idle()
    assert runtime.agent.messages[-1]["content"] == "retain this input"
    assert "Runtime operation failed" in caplog.text
    assert not runtime.client.state.running


def test_chat_failure_recording_error_does_not_disconnect_backend(runtime, caplog):
    def fail():
        raise RuntimeError("primary operation failure")

    def fail_recording(_error):
        raise ValueError("diagnostic persistence failure")

    runtime.loop.run = fail
    runtime.server.commands.record_chat_failure = fail_recording

    runtime.client.submit("trigger failure")

    with pytest.raises(RpcError, match="primary operation failure"):
        runtime.client.wait_idle()

    assert "Failed to record chat failure" in caplog.text
    assert not runtime.client.state.running

    # A secondary persistence failure must not close the RPC peer.
    runtime.client.submit("/help")
    runtime.client.wait_idle()

    assert not runtime.client.state.running


def test_invalid_wire_action_does_not_start_an_operation(runtime):
    with pytest.raises(RpcError) as error:
        runtime.client.submit(ActionRequest("thinking.set_effort", {"level": []}))
    assert error.value.code == -32602
    assert not runtime.client.state.running


def test_new_input_clears_previous_stop_in_backend(runtime):
    runtime.agent.request_stop()
    runtime.client.submit("/help")
    runtime.client.wait_idle()
    assert not runtime.agent.stop_requested()


def test_resize_is_scoped_by_backend_session(runtime):
    from types import SimpleNamespace

    calls = []
    done = threading.Event()
    runtime.agent.process_manager = SimpleNamespace(
        resize_tty_sessions=lambda **kwargs: (calls.append(kwargs), done.set())
    )
    runtime.client.resize(24, 80)
    assert done.wait(2)
    assert calls == [
        dict(
            rows=24,
            columns=80,
            agent_id=runtime.agent.agent_id,
            owner_session_id="test-session",
            session_generation=runtime.agent.session_generation,
        )
    ]


def test_shutdown_is_idempotent(runtime):
    runtime.client.submit("retain this")
    runtime.client.wait_idle()
    first = runtime.client.shutdown()
    messages = list(runtime.agent.messages)
    assert runtime.client.shutdown() == first
    assert runtime.agent.messages == messages


def test_shutdown_reports_progress_before_slow_manifest_commit(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from reuleauxcoder.infrastructure.persistence.session_store import SessionStore

    directory = runtime.server.commands.sessions_dir / runtime.server.commands.session_id
    directory.mkdir()
    runtime.agent.history_ledger.bind_jsonl(directory / "events.jsonl")
    runtime.client.submit("retain this")
    runtime.client.wait_idle()
    runtime.config.session_auto_save = True
    progress = []
    runtime.client.peer.notifications["runtime.shutdown_progress"] = (
        lambda message: progress.append(message)
    )
    committing, release = threading.Event(), threading.Event()
    original = SessionStore._atomic_write_json

    def write(store, path, payload, **kwargs):
        if kwargs["ref"] == "manifest":
            committing.set()
            assert release.wait(5)
        return original(store, path, payload, **kwargs)

    monkeypatch.setattr(SessionStore, "_atomic_write_json", write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(runtime.client.shutdown)
        try:
            assert committing.wait(5)
            runtime.client.peer.wait_notifications()
            assert progress[0] == "Stopping active tasks..."
            assert "Writing replay snapshot" in "\n".join(progress)
            assert progress[-1] == "Committing session manifest..."
            assert not pending.done()
            assert runtime.server.commands.exit_saved_session_id is None
        finally:
            release.set()
        saved = pending.result(timeout=5)
    assert saved == runtime.server.commands.exit_saved_session_id
    restored = SessionStore(runtime.server.commands.sessions_dir).load(saved)
    assert restored is not None
    assert not restored.restore_issues


def test_shutdown_cancels_an_interaction_before_cli_pumps_it(runtime):
    runtime.client._foreground_interactions = True
    responses = []
    runtime.loop.run = lambda: responses.append(
        runtime.agent.ui_interactor.confirm(ConfirmRequest("Confirm", "Pending"))
    )
    runtime.client.submit("ask")
    # A queued prompt has no active terminal adapter yet.
    _, _, future = runtime.client._interaction_queue.get(timeout=2)
    runtime.client.shutdown()
    assert future.result(timeout=2).cancelled
    assert responses[0].cancelled


def test_cli_pumps_reverse_interactions_on_its_own_thread(runtime):
    runtime.client._foreground_interactions = True
    threads = []
    from reuleauxcoder.app.interaction_contracts import ConfirmResponse

    runtime.interactor.confirm = lambda request: (
        threads.append(threading.get_ident()),
        ConfirmResponse(True),
    )[1]
    runtime.loop.run = lambda: runtime.agent.ui_interactor.confirm(
        ConfirmRequest("Confirm", "Question")
    )
    runtime.client.submit("ask")
    runtime.client.wait_idle()
    assert threads == [threading.get_ident()]


def test_shutdown_saves_retained_content_after_runtime_failure(runtime):
    from reuleauxcoder.infrastructure.persistence.session_store import SessionStore

    runtime.config.session_auto_save = True

    def fail():
        raise ValueError("deliberate test failure")

    runtime.loop.run = fail
    runtime.client.submit("persist this input")
    with pytest.raises(RpcError):
        runtime.client.wait_idle()
    saved_id = runtime.client.shutdown()
    session = SessionStore(runtime.server.commands.sessions_dir).load(saved_id)
    assert any(
        message.get("content") == "persist this input" for message in session.messages
    )


def test_frontend_performance_samples_reach_backend_monitor(runtime):
    from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor

    monitor = runtime.agent.performance_monitor = RuntimePerformanceMonitor()
    recorded = threading.Event()
    original = monitor.record
    monitor.record = lambda *args, **kwargs: (
        original(*args, **kwargs),
        recorded.set(),
    )[0]
    runtime.client.record_performance("ui_queue", "drain", 3.5, attributes={"depth": 2})
    assert recorded.wait(2)
    assert monitor.snapshot()[0].attribute_map()["depth"] == 2
