import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.help import build_help_view
from reuleauxcoder.app.commands.loader import create_builtin_action_registry

from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.extensions import LifecycleCoordinator
from reuleauxcoder.domain.session.models import Session, SessionRuntimeState
from reuleauxcoder.extensions.command.builtin.sessions import (
    ListSessionsCommand,
    NewSessionCommand,
    ResumeSessionCommand,
    _handle_list_sessions,
    _handle_new_session,
    _handle_resume_session,
    _parse_list_sessions,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.ui_events import UIEventBus, UIEventKind, ViewEventPayload
from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.rpc.codec import encode
from reuleauxcoder.infrastructure.persistence.history_query import SessionHistory
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport


class FakeLLM:
    def __init__(self) -> None:
        self.model = "base-model"
        self.debug_trace = False

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000

    def reconfigure(self, max_tokens: int, **_strategy_settings) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self) -> None:
        self.current_session_id = None
        self.session_generation = 0
        self.llm = FakeLLM()
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {"coder": SimpleNamespace(name="coder", description="")}
        self.active_mode = None
        self.hook_registry = HookRegistry()
        self.lifecycle = LifecycleCoordinator(self.hook_registry)
        self.runtime_issues: list[tuple[str, str, str]] = []

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def reset(self) -> None:
        self.session_generation += 1
        self.state.messages.clear()
        self.messages = self.state.messages
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.current_round = 0

    def record_runtime_issue(self, phase: str, error_type: str, ref: str) -> None:
        self.runtime_issues.append((phase, error_type, ref))


def _build_ctx(tmp_path: Path, *, fingerprint: str = "local") -> SimpleNamespace:
    config = Config(api_key="key", approval=ApprovalConfig(), session_dir=str(tmp_path))
    agent = FakeAgent()
    setattr(agent, "session_fingerprint", fingerprint)
    effect = CommandEffect()
    return SimpleNamespace(
        config=config, agent=agent, effect=effect, sessions_dir=tmp_path
    )


def _run_command(ctx, user_input: str, ui_bus: UIEventBus):
    return CommandService(
        ctx.agent,
        ctx.config,
        ui_bus,
        UIProfile(
            ui_id="cli",
            display_name="CLI",
            capabilities=frozenset({UICapability.TEXT_INPUT}),
        ),
        create_builtin_action_registry(),
        sessions_dir=ctx.sessions_dir,
    ).submit(user_input)


def test_list_sessions_defaults_to_current_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(), ctx)

    assert [item["id"] for item in result.state["sessions"]] == [local_id]
    assert result.state["show_all"] is False
    assert result.state["fingerprint"] == "local"
    assert result.state["sessions"][0]["position"] == 1


def test_session_without_target_is_the_canonical_list_command() -> None:
    assert isinstance(_parse_list_sessions("/session", None), ListSessionsCommand)
    command = _parse_list_sessions("/session all", None)
    assert isinstance(command, ListSessionsCommand)
    assert command.show_all is True


def test_session_help_uses_only_the_canonical_singular_surface() -> None:
    profile = UIProfile(
        ui_id="cli",
        display_name="CLI",
        capabilities=frozenset({UICapability.TEXT_INPUT}),
    )

    view = build_help_view(profile, create_builtin_action_registry())

    section = next(item for item in view.sections if item.feature_id == "sessions")
    usages = {command.usage for command in section.commands}
    assert "/session" in usages
    assert "/session <#|id|latest>" in usages
    assert all(not usage.startswith("/sessions") for usage in usages)


def test_list_sessions_all_shows_all_fingerprints(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
    )
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(show_all=True), ctx)

    assert {item["id"] for item in result.state["sessions"]} == {local_id, remote_id}
    assert result.state["show_all"] is True
    assert {item["fingerprint"] for item in result.state["sessions"]} == {
        "local",
        "remote:abc",
    }


def test_resume_latest_uses_current_fingerprint_only(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
        runtime_state=SessionRuntimeState(model="m1", active_mode="coder"),
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(model="m2", active_mode="coder"),
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_resume_session(ResumeSessionCommand(target="latest"), ctx)

    assert result.session_id == local_id
    assert ctx.agent.session_fingerprint == "local"
    assert any(
        event.level == "success"
        and event.kind == UIEventKind.SESSION.value
        and local_id in event.message
        for event in ctx.effect.notifications
    )
    transcript = next(
        view for view in ctx.effect.views if view.view_type == "session_resume"
    )
    assert transcript.view_model.entries[0].content == "local msg"


def test_resume_preview_is_bounded_and_full_content_remains_pageable(tmp_path):
    text = '中文🙂"\\\n' * 20_000
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": text},
        ],
        model="test",
    )
    ctx = _build_ctx(tmp_path)
    _handle_resume_session(ResumeSessionCommand(target=session_id), ctx)
    transcript = next(
        view for view in ctx.effect.views if view.view_type == "session_resume"
    )
    assert "Preview truncated" in transcript.view_model.entries[-1].content
    assert ctx.agent.state.messages[-1]["content"] == text
    # Exercise the typed resume-view encoding and the same framing as stdio.
    output = io.BytesIO()
    payload = ViewEventPayload(
        action=transcript.action, title=transcript.title,
        view_model=transcript.view_model,
    )
    StreamTransport(io.BytesIO(), output).send(
        json.dumps(encode(payload), ensure_ascii=False)
    )
    assert len(output.getvalue()) < StreamTransport.MAX_MESSAGE_BYTES
    loaded = store.load(session_id)
    assert loaded.messages[-1]["content"] == text
    event = next(event for event in loaded.history_events if event.role == "assistant")
    history = SessionHistory(tmp_path)
    cursor, parts = None, []
    while True:
        page = history.read(session_id, event_id=event.event_id, cursor=cursor)
        parts.extend(record.content for record in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert "".join(parts) == text


def test_corrupt_resume_keeps_current_session_and_exposes_safe_failure_fact(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    target_id = store.save(
        messages=[{"role": "user", "content": "target"}], model="model"
    )
    sentinel = "resume-command-secret-must-not-leak"
    (tmp_path / target_id / "replay.json").write_text(
        '{"broken":"' + sentinel,
        encoding="utf-8",
    )
    ctx = _build_ctx(tmp_path)
    ctx.agent.current_session_id = "current-session"
    ctx.agent.messages.append({"role": "user", "content": "current work"})
    ui_bus = UIEventBus()

    result = _run_command(ctx, f"/session {target_id}", ui_bus)

    assert result.control == "continue"
    assert result.session_id == "current-session"
    assert ctx.agent.current_session_id == "current-session"
    assert ctx.agent.messages[-1]["content"] == "current work"
    assert ctx.agent.runtime_issues == [("replay_decode", "JSONDecodeError", "replay")]
    rendered = "\n".join(event.message for event in ui_bus.history_snapshot())
    assert "phase=replay_decode" in rendered
    assert sentinel not in rendered


def test_missing_resume_is_a_model_visible_failed_command(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.agent.current_session_id = "current-session"
    ctx.agent.messages.append({"role": "user", "content": "current work"})
    ui_bus = UIEventBus()

    result = _run_command(ctx, "/session session_missing", ui_bus)

    assert result.control == "continue"
    assert result.session_id == "current-session"
    assert ctx.agent.messages[-1]["content"] == "current work"
    assert ctx.agent.runtime_issues == [
        ("session_discovery", "FileNotFoundError", "session")
    ]
    rendered = "\n".join(event.message for event in ui_bus.history_snapshot())
    assert "phase=session_discovery" in rendered
    assert "error_type=FileNotFoundError" in rendered


def test_resume_by_list_number_replays_recent_human_turns(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = []
    for index in range(1, 5):
        messages.extend(
            (
                {"role": "user", "content": f"question {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            )
        )
    messages.append(
        {"role": "user", "content": "[SESSION_EXIT] User left the session."}
    )
    session_id = store.save(messages=messages, model="m1", fingerprint="local")
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_resume_session(ResumeSessionCommand(target="1"), ctx)

    assert result.session_id == session_id
    transcript = next(
        view.view_model
        for view in ctx.effect.views
        if view.view_type == "session_resume"
    )
    assert [entry.content for entry in transcript.entries] == [
        "question 2",
        "answer 2",
        "question 3",
        "answer 3",
        "question 4",
        "answer 4",
    ]


def test_resume_by_invalid_list_number_points_back_to_session_list(
    tmp_path: Path,
) -> None:
    ctx = _build_ctx(tmp_path)

    result = _handle_resume_session(ResumeSessionCommand(target="2"), ctx)

    assert result.session_id is None
    assert "/session to list" in ctx.effect.notifications[0].message


def test_resume_cross_fingerprint_by_id_warns_but_allows(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(model="m2", active_mode="coder"),
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_resume_session(ResumeSessionCommand(target=remote_id), ctx)

    assert result.session_id == remote_id
    assert ctx.agent.session_fingerprint == "remote:abc"
    assert any(
        event.level == "warning"
        and event.kind == UIEventKind.SESSION.value
        and "belongs to fingerprint 'remote:abc'" in event.message
        for event in ctx.effect.notifications
    )


def test_resume_auto_saves_the_session_being_left(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    target_id = store.save(messages=[{"role": "user", "content": "target"}], model="m1")
    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "unsaved current work"})

    ctx.agent.current_session_id = "current"
    result = _handle_resume_session(ResumeSessionCommand(target=target_id), ctx)

    assert result.session_id == target_id
    saved_current = store.load("current")
    assert saved_current is not None
    assert saved_current.get_preview() == "unsaved current work"


def test_service_queued_save_follows_new_session_and_returns_transition(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.current_session_id = "old"
    ctx.agent.messages.append({"role": "user", "content": "old work"})
    commands = CommandService(
        ctx.agent,
        ctx.config,
        UIEventBus(),
        UIProfile("tui", "TUI", frozenset(UICapability)),
        create_builtin_action_registry(),
        sessions_dir=tmp_path,
    )

    commands.prepare_during_turn("/save")
    result = commands.submit("/new")
    assert result.clear_transcript and result.session_changed
    assert result.session_id == commands.session_id != "old"
    ctx.agent.messages.append({"role": "user", "content": "new work"})
    commands.submit(commands.next_pending())

    assert SessionStore(tmp_path).load(result.session_id).get_preview() == "new work"
    assert SessionStore(tmp_path).load("old") is None


def test_service_resume_marker_is_consumed_once_and_reset_clears_it(tmp_path):
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    store = SessionStore(tmp_path)
    sid = store.save([{"role": "user", "content": "saved"}], "m", is_exit=True)
    commands = CommandService(
        ctx.agent,
        ctx.config,
        UIEventBus(),
        UIProfile("cli", "CLI", frozenset(UICapability)),
        create_builtin_action_registry(),
        sessions_dir=tmp_path,
    )

    assert commands.submit(f"/session {sid}").session_changed
    assert commands.prepare_chat_input("continue").startswith("[SESSION_RESUME]")
    assert commands.prepare_chat_input("again") == "again"
    assert commands.submit(f"/session {sid}").session_changed
    commands.submit("/reset")
    assert commands.prepare_chat_input("fresh") == "fresh"


def test_service_exit_preserves_content_once_even_when_observer_crashes(
    tmp_path, caplog
):
    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "keep this"})
    observed = []

    def saved(sid):
        observed.append(sid)
        raise RuntimeError("observer failed")

    ctx.agent.lifecycle = SimpleNamespace(session_saved=saved)
    commands = CommandService(
        ctx.agent,
        ctx.config,
        UIEventBus(),
        UIProfile("cli", "CLI", frozenset(UICapability)),
        create_builtin_action_registry(),
        sessions_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="observer failed"):
        commands.submit("/quit")
    sid = commands.exit_saved_session_id
    assert commands.save_exit() == sid == commands.session_id
    assert observed == [sid]
    loaded = SessionStore(tmp_path).load(sid)
    assert loaded.get_preview() == "keep this"
    assert loaded.fingerprint == "local"
    assert loaded.model == ctx.agent.llm.model
    assert caplog.records[-1].getMessage() == "Command failed: system.exit"


def test_new_session_respects_disabled_auto_save(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.messages.append({"role": "user", "content": "do not persist"})

    result = _handle_new_session(NewSessionCommand(), ctx)

    assert result.session_id is not None
    assert SessionStore(tmp_path).list() == []
    assert ctx.agent.messages == []


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_save_callback_failure_propagates_after_preserving_content(
    tmp_path: Path,
    caplog,
    error_type,
) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.agent.current_session_id = "current-session"
    ctx.agent.messages.append({"role": "user", "content": "work to preserve"})
    error = error_type("save observer failed")

    def fail_callback(*args, **kwargs):
        raise error

    ctx.agent.lifecycle = SimpleNamespace(session_saved=fail_callback)
    ui_bus = UIEventBus()

    with pytest.raises(error_type) as raised:
        _run_command(ctx, "/save", ui_bus)

    assert raised.value is error
    saved = SessionStore(tmp_path).load("current-session")
    assert saved.get_preview() == "work to preserve"
    assert not ui_bus.history_snapshot()
    if error_type is KeyboardInterrupt:
        assert not caplog.records
    else:
        record = caplog.records[-1]
        assert record.getMessage() == "Command failed: sessions.save"
        assert record.exc_info[1] is error
        assert "fail_callback" in caplog.text


@pytest.mark.parametrize(
    "owner, method",
    [(SessionStore, "get_exit_time"), (Session, "get_recent_conversation")],
)
def test_resume_projection_failure_is_logged_and_propagates(
    tmp_path: Path,
    monkeypatch,
    caplog,
    owner,
    method,
) -> None:
    store = SessionStore(tmp_path)
    target_id = store.save(
        messages=[{"role": "user", "content": "saved work"}],
        model="m1",
        fingerprint="local",
    )
    ctx = _build_ctx(tmp_path)
    ctx.agent.current_session_id = "current-session"
    error = RuntimeError("projection failed")

    def fail_projection(*args, **kwargs):
        raise error

    monkeypatch.setattr(owner, method, fail_projection)
    ui_bus = UIEventBus()

    with pytest.raises(RuntimeError) as raised:
        _run_command(ctx, f"/session {target_id}", ui_bus)

    assert raised.value is error
    assert caplog.records[-1].exc_info[1] is error
    assert not ui_bus.history_snapshot()
    assert store.load(target_id).get_preview() == "saved work"


def test_restore_issue_recorder_failure_is_logged_and_propagates(
    tmp_path: Path,
    caplog,
) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.agent.current_session_id = "current-session"
    error = RuntimeError("recorder failed")

    def fail_recording(*args):
        raise error

    ctx.agent.record_runtime_issue = fail_recording

    with pytest.raises(RuntimeError) as raised:
        _run_command(ctx, "/session missing", UIEventBus())

    assert raised.value is error
    assert caplog.records[-1].exc_info[1] is error
    assert "fail_recording" in caplog.text
