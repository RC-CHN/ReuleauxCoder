import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.agent import RuntimeIssue
from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.domain.context.manager import MESSAGE_TOKEN_KEY
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.session.models import SessionRestoreIssue, SessionRuntimeState
from reuleauxcoder.infrastructure.persistence.session_store import (
    SessionInventoryResult,
    SessionRestoreError,
    SessionStore,
)
from reuleauxcoder.infrastructure.persistence.session_projection import (
    INDEX_DIRECTORY_NAME,
)
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)
from reuleauxcoder.app.ui_events import UIEventBus, UIEventKind, UIEventLevel


def _session_entry_names(path: Path) -> set[str]:
    return {
        entry.name
        for entry in path.iterdir()
        if entry.name != INDEX_DIRECTORY_NAME
    }


class FakeLLM:
    def __init__(self) -> None:
        self.model = "base-model"
        self.debug_trace = False
        self.api_key = "key"
        self.base_url = None
        self.temperature = 0.0
        self.max_tokens = 2048

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000

    def reconfigure(self, max_tokens: int, **_strategy_settings) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self, fingerprint: str = "local") -> None:
        self.llm = FakeLLM()
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        }
        self.active_mode = None
        self.session_fingerprint = fingerprint
        self.active_main_model_profile = None
        self.active_sub_model_profile = None
        self.hook_registry = HookRegistry()
        self.runtime_issues: list[RuntimeIssue] = []

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def record_runtime_issue(
        self,
        phase: str,
        error_type: str,
        ref: str,
        count: int = 1,
    ) -> None:
        self.runtime_issues.append(RuntimeIssue(phase, error_type, ref, count))


def _build_config(tmp_path: Path) -> Config:
    return Config(
        api_key="key",
        approval=ApprovalConfig(default_mode="require_approval"),
        session_dir=str(tmp_path),
        modes={
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        },
        active_mode="coder",
        llm_debug_trace=False,
    )


def _build_runner(*, startup_progress=None, **options) -> AppRunner:
    return AppRunner(
        options=AppOptions(**options),
        dependencies=AppDependencies(
            create_session_store=lambda sessions_dir: SessionStore(sessions_dir)
        ),
        startup_progress=startup_progress,
    )


def test_restore_session_auto_resume_latest_is_fingerprint_scoped(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="local-model",
        fingerprint="local",
        runtime_state=SessionRuntimeState(
            model="local-model", active_mode="debugger", llm_debug_trace=True
        ),
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="remote-model",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(
            model="remote-model", active_mode="coder", llm_debug_trace=False
        ),
    )
    startup_progress = []
    runner = _build_runner(
        auto_resume_latest=True, startup_progress=startup_progress.append
    )
    config = _build_config(tmp_path)
    agent = FakeAgent(fingerprint="local")
    ui_bus = UIEventBus()

    current_session_id, session_exit_time, sessions_dir = runner._restore_session(
        config, agent, ui_bus
    )

    assert current_session_id == local_id
    assert session_exit_time is None
    assert sessions_dir == tmp_path
    assert agent.session_fingerprint == "local"
    assert agent.active_mode == "debugger"
    assert agent.llm.model == "local-model"
    assert agent.llm.debug_trace is True
    assert "Looking for the latest compatible session..." in startup_progress
    assert f"Restoring latest session {local_id}..." in startup_progress
    assert any(
        message.startswith("Reading history ledger (") for message in startup_progress
    )
    assert any(
        message.startswith("Restored 1 message(s) and ") for message in startup_progress
    )
    assert any(
        event.level == UIEventLevel.INFO
        and event.kind == UIEventKind.SESSION
        and f"Auto-resumed latest session: {local_id}" in event.message
        for event in ui_bus._history
    )


def test_auto_resume_uses_latest_and_issues_from_the_same_inventory_scan(
    tmp_path: Path,
) -> None:
    backing = SessionStore(tmp_path)
    session_id = backing.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    metadata = backing.get_latest_result(fingerprint="local").session
    issue = SessionRestoreIssue(
        phase="manifest_decode",
        error_type="JSONDecodeError",
        ref="manifest",
    )

    class AtomicStore:
        def set_progress_callback(self, _progress) -> None:
            pass

        def list_result(self, *, limit, fingerprint):
            assert fingerprint == "local"
            assert limit == 1
            return SessionInventoryResult(sessions=(metadata,), issues=(issue,))

        @property
        def inventory_issues(self):
            raise AssertionError("must not consult mutable last-scan state")

        def load(self, requested_id):
            assert requested_id == session_id
            return backing.load(requested_id)

        def get_exit_time(self, messages):
            return backing.get_exit_time(messages)

    runner = AppRunner(
        options=AppOptions(auto_resume_latest=True),
        dependencies=AppDependencies(
            create_session_store=lambda _sessions_dir: AtomicStore()
        ),
    )
    agent = FakeAgent()

    restored_id, _, _ = runner._restore_session(
        _build_config(tmp_path), agent, UIEventBus()
    )

    assert restored_id == session_id
    assert agent.session_inventory_issues == (issue,)


@pytest.mark.parametrize("source", ["progress", "load_progress", "ui_bus"])
@pytest.mark.parametrize("error_type", [ValueError, OSError, SystemExit])
def test_restore_callback_failure_is_logged_and_propagated(
    tmp_path: Path, caplog, source, error_type
) -> None:
    messages = [{"role": "user", "content": "authoritative"}]
    SessionStore(tmp_path).save(messages=messages, model="model")
    failure = error_type("restore callback failed")

    class FailingUIBus(UIEventBus):
        def emit(self, _event) -> None:
            raise failure

    def fail_progress(message: str) -> None:
        if source == "progress" or message.startswith("Loading session files"):
            raise failure

    runner = _build_runner(
        startup_progress=fail_progress if source != "ui_bus" else None,
        auto_resume_latest=True,
    )
    agent = FakeAgent()
    bus = FailingUIBus() if source == "ui_bus" else UIEventBus()

    with pytest.raises(error_type) as raised:
        runner._restore_session(_build_config(tmp_path), agent, bus)

    assert raised.value is failure
    record = caplog.records[-1]
    ref = "ui_bus" if source == "ui_bus" else "progress_callback"
    assert f"restore_observer/{ref}" in record.message
    assert record.exc_info[1] is failure
    assert record.exc_info[2] is not None
    assert "restore callback failed" in caplog.text
    # A notification failure must not roll back an already restored transcript.
    assert [message["content"] for message in agent.state.messages] == (
        ["authoritative"] if source == "ui_bus" else []
    )


def test_restore_progress_keyboard_interrupt_remains_user_control(
    tmp_path: Path,
) -> None:
    SessionStore(tmp_path).save(
        messages=[{"role": "user", "content": "saved"}],
        model="model",
    )

    def interrupt_progress(_message: str) -> None:
        raise KeyboardInterrupt

    runner = _build_runner(
        startup_progress=interrupt_progress,
        auto_resume_latest=True,
    )

    with pytest.raises(KeyboardInterrupt):
        runner._restore_session(_build_config(tmp_path), FakeAgent(), UIEventBus())


def test_auto_resume_warns_and_skips_unreadable_inventory_entries(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local"}],
        model="model",
        fingerprint="local",
    )
    foreign_id = store.save(
        messages=[{"role": "user", "content": "remote"}],
        model="model",
        fingerprint="remote:peer",
    )
    (tmp_path / foreign_id / "manifest.json").write_text('{"broken":', encoding="utf-8")
    runner = _build_runner(auto_resume_latest=True)
    agent = FakeAgent(fingerprint="local")
    ui_bus = UIEventBus()

    resumed, _, _ = runner._restore_session(_build_config(tmp_path), agent, ui_bus)

    assert resumed == local_id
    assert agent.state.messages[0]["content"] == "local"
    assert any(event.level == UIEventLevel.WARNING and event.data.get("phase") == "manifest_decode" for event in ui_bus.history_snapshot())
    assert (tmp_path / foreign_id / "manifest.json").read_text() == '{"broken":'


def test_restore_session_manual_resume_warns_on_cross_fingerprint_and_restores_runtime(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="remote-model",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(
            model="remote-model",
            active_mode="debugger",
            llm_debug_trace=True,
            approval_rules=[{"tool_name": "shell", "action": "deny"}],
        ),
    )
    runner = _build_runner(resume_session_id=remote_id, auto_resume_latest=False)
    config = _build_config(tmp_path)
    agent = FakeAgent(fingerprint="local")
    ui_bus = UIEventBus()

    current_session_id, _, _ = runner._restore_session(config, agent, ui_bus)

    assert current_session_id == remote_id
    assert agent.session_fingerprint == "remote:abc"
    assert agent.active_mode == "debugger"
    assert agent.llm.model == "remote-model"
    assert agent.llm.debug_trace is True
    assert [
        (rule.tool_name, rule.action)
        for rule in getattr(agent, "session_approval_rules")
    ] == [("shell", "deny")]
    assert any(
        event.level == UIEventLevel.WARNING
        and event.kind == UIEventKind.SESSION
        and "belongs to fingerprint 'remote:abc'" in event.message
        for event in ui_bus._history
    )
    assert any(
        event.level == UIEventLevel.SUCCESS
        and event.kind == UIEventKind.SESSION
        and f"Resumed session: {remote_id}" in event.message
        for event in ui_bus._history
    )


def test_explicit_resume_propagates_safe_canonical_failure_without_new_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    sentinel = "resume-secret-must-not-leak"
    (tmp_path / session_id / "replay.json").write_text(
        '{"broken":"' + sentinel,
        encoding="utf-8",
    )
    runner = _build_runner(
        resume_session_id=session_id,
        auto_resume_latest=False,
    )
    config = _build_config(tmp_path)
    agent = FakeAgent()

    with pytest.raises(SessionRestoreError) as raised:
        runner._restore_session(config, agent, UIEventBus())

    error = raised.value
    assert error.phase == "replay_decode"
    assert error.error_type == "JSONDecodeError"
    assert error.ref == "replay"
    assert sentinel not in str(error)
    assert getattr(agent, "current_session_id", None) is None
    assert _session_entry_names(tmp_path) == {session_id}


def test_explicit_resume_missing_fails_without_generating_new_session(
    tmp_path: Path,
) -> None:
    runner = _build_runner(
        resume_session_id="session_missing",
        auto_resume_latest=False,
    )
    agent = FakeAgent()

    with pytest.raises(SessionRestoreError) as raised:
        runner._restore_session(_build_config(tmp_path), agent, UIEventBus())

    assert raised.value.phase == "session_discovery"
    assert raised.value.error_type == "FileNotFoundError"
    assert raised.value.ref == "session"
    assert getattr(agent, "current_session_id", None) is None
    assert list(tmp_path.iterdir()) == []


def test_auto_resume_selected_session_disappearing_starts_new_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )

    class VanishingStore:
        def set_progress_callback(self, _progress) -> None:
            pass

        def list_result(self, *, limit, fingerprint):
            return store.list_result(limit=limit, fingerprint=fingerprint)

        def load(self, _session_id):
            return None

        def generate_session_id(self):
            return "replacement-session"

    vanishing = VanishingStore()
    runner = AppRunner(
        options=AppOptions(auto_resume_latest=True),
        dependencies=AppDependencies(create_session_store=lambda _path: vanishing),
    )
    agent = FakeAgent()

    ui_bus = UIEventBus()
    restored, _, _ = runner._restore_session(_build_config(tmp_path), agent, ui_bus)

    assert restored == agent.current_session_id == "replacement-session"
    assert agent.session_restore_issues == ()
    assert any(event.data.get("phase") == "session_load" and event.data.get("error_type") == "FileNotFoundError" for event in ui_bus.history_snapshot())
    assert any(event.level == UIEventLevel.WARNING and "Starting a new session" in event.message for event in ui_bus.history_snapshot())
    assert _session_entry_names(tmp_path) == {session_id}


@pytest.mark.parametrize("artifact", ["manifest", "replay"])
@pytest.mark.parametrize("missing", [False, True])
def test_auto_resume_read_failure_warns_and_preserves_original_files(
    tmp_path: Path, artifact: str, missing: bool,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    sentinel = "auto-resume-secret-must-not-leak"
    path = tmp_path / session_id / f"{artifact}.json"
    if missing:
        path.unlink()
    else:
        path.write_text('{"broken":"' + sentinel, encoding="utf-8")
    original = {p: p.read_bytes() for p in (tmp_path / session_id).rglob("*") if p.is_file()}
    runner = _build_runner(auto_resume_latest=True)
    agent = FakeAgent()

    ui_bus = UIEventBus()
    restored, _, _ = runner._restore_session(_build_config(tmp_path), agent, ui_bus)

    assert restored == agent.current_session_id and restored != session_id
    assert agent.messages == []
    assert agent.active_mode == "coder"
    warnings = [event for event in ui_bus.history_snapshot() if event.level == UIEventLevel.WARNING]
    assert any(event.data.get("phase") == f"{artifact}_{'read' if missing else 'decode'}" for event in warnings)
    assert any("original files were kept" in event.message for event in warnings)
    assert sentinel not in str(warnings)
    assert {p: p.read_bytes() for p in (tmp_path / session_id).rglob("*") if p.is_file()} == original
    assert _session_entry_names(tmp_path) == {session_id}


def test_auto_resume_marks_history_corruption_as_degraded(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    sentinel = "lifecycle-history-secret-must-not-leak"
    with (tmp_path / session_id / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"broken":"' + sentinel + "\n")
    runner = _build_runner(auto_resume_latest=True)
    agent = FakeAgent()
    ui_bus = UIEventBus()

    current_session_id, _, _ = runner._restore_session(
        _build_config(tmp_path), agent, ui_bus
    )

    assert current_session_id == session_id
    issues = tuple(agent.session_restore_issues)
    assert any(issue.phase == "history_decode" for issue in issues)
    assert any(
        event.level == UIEventLevel.WARNING
        and event.data.get("phase") == "history_decode"
        for event in ui_bus.history_snapshot()
    )
    rendered = "\n".join(event.message for event in ui_bus.history_snapshot())
    assert "Auto-resumed latest session with degraded recovery" in rendered
    assert sentinel not in rendered


def test_auto_resume_exposes_recomputed_token_projection_to_agent(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative replay"}],
        model="model",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["message_token_counts"] = [-1]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner = _build_runner(auto_resume_latest=True)
    agent = FakeAgent()
    ui_bus = UIEventBus()

    current_session_id, _, _ = runner._restore_session(
        _build_config(tmp_path), agent, ui_bus
    )

    assert current_session_id == session_id
    assert agent.state.messages[0]["content"] == "authoritative replay"
    assert isinstance(agent.state.messages[0][MESSAGE_TOKEN_KEY], int)
    assert any(
        issue.error_type == "MessageTokenCountsValidationError"
        for issue in agent.session_restore_issues
    )
    assert any(
        event.level == UIEventLevel.WARNING
        and event.data.get("error_type") == "MessageTokenCountsValidationError"
        for event in ui_bus.history_snapshot()
    )


def test_missing_persisted_approval_action_cannot_inherit_allow_default(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}],
        model="model",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"]["approval_rules"] = [
        {"tool_name": "execute_command"}
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = _build_config(tmp_path)
    config.approval.default_mode = "allow"
    runner = _build_runner(resume_session_id=session_id, auto_resume_latest=False)
    agent = FakeAgent()

    with pytest.raises(SessionRestoreError) as raised:
        runner._restore_session(config, agent, UIEventBus())

    assert raised.value.phase == "manifest_validate"
    assert raised.value.error_type == "SessionRuntimeStateValidationError"
    assert getattr(agent, "session_approval_rules", ()) == ()
    assert getattr(agent, "current_session_id", None) is None
    assert _session_entry_names(tmp_path) == {session_id}


def test_invalid_persisted_plan_fails_before_mutating_live_agent(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "persisted"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"]["plan_state"] = {
        "items": [
            {"step": "one", "active_form": "one", "status": "in_progress"},
            {"step": "two", "active_form": "two", "status": "in_progress"},
        ]
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    agent = FakeAgent()
    agent.messages.append({"role": "user", "content": "live state"})
    agent.active_mode = "debugger"
    runner = _build_runner(resume_session_id=session_id, auto_resume_latest=False)

    with pytest.raises(SessionRestoreError) as raised:
        runner._restore_session(_build_config(tmp_path), agent, UIEventBus())

    assert raised.value.error_type == "SessionRuntimeStateValidationError"
    assert agent.messages == [{"role": "user", "content": "live state"}]
    assert agent.active_mode == "debugger"
    assert getattr(agent, "current_session_id", None) is None
