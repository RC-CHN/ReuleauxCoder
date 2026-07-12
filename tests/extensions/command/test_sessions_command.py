from pathlib import Path
from types import SimpleNamespace
from reuleauxcoder.app.commands.models import CommandEffect

from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.extensions import LifecycleCoordinator
from reuleauxcoder.domain.session.models import SessionRuntimeState
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
from reuleauxcoder.interfaces.events import UIEventKind


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

    def reconfigure(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self) -> None:
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

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def reset(self) -> None:
        self.state.messages.clear()
        self.messages = self.state.messages
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.current_round = 0


def _build_ctx(tmp_path: Path, *, fingerprint: str = "local") -> SimpleNamespace:
    config = Config(api_key="key", approval=ApprovalConfig(), session_dir=str(tmp_path))
    agent = FakeAgent()
    setattr(agent, "session_fingerprint", fingerprint)
    effect = CommandEffect()
    return SimpleNamespace(
        config=config, agent=agent, effect=effect, sessions_dir=tmp_path
    )


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
    target_id = store.save(
        messages=[{"role": "user", "content": "target"}], model="m1"
    )
    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "unsaved current work"})

    result = _handle_resume_session(
        ResumeSessionCommand(target=target_id, current_session_id="current"), ctx
    )

    assert result.session_id == target_id
    saved_current = store.load("current")
    assert saved_current is not None
    assert saved_current.get_preview() == "unsaved current work"


def test_new_session_respects_disabled_auto_save(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.messages.append({"role": "user", "content": "do not persist"})

    result = _handle_new_session(NewSessionCommand(current_session_id=None), ctx)

    assert result.session_id is not None
    assert SessionStore(tmp_path).list() == []
    assert ctx.agent.messages == []
