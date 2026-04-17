from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.session.models import SessionRuntimeState
from reuleauxcoder.extensions.command.builtin.sessions import (
    ListSessionsCommand,
    ResumeSessionCommand,
    _handle_list_sessions,
    _handle_resume_session,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel


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
    ui_bus = UIEventBus()
    return SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus, sessions_dir=tmp_path)


def test_list_sessions_defaults_to_current_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(messages=[{"role": "user", "content": "local msg"}], model="m1", fingerprint="local")
    store.save(messages=[{"role": "user", "content": "remote msg"}], model="m2", fingerprint="remote:abc")
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(), ctx)

    assert [item["id"] for item in result.payload["sessions"]] == [local_id]
    assert result.payload["show_all"] is False
    assert result.payload["fingerprint"] == "local"


def test_list_sessions_all_shows_all_fingerprints(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(messages=[{"role": "user", "content": "local msg"}], model="m1", fingerprint="local")
    remote_id = store.save(messages=[{"role": "user", "content": "remote msg"}], model="m2", fingerprint="remote:abc")
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(show_all=True), ctx)

    assert {item["id"] for item in result.payload["sessions"]} == {local_id, remote_id}
    assert result.payload["show_all"] is True
    assert {item["fingerprint"] for item in result.payload["sessions"]} == {"local", "remote:abc"}


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
        event.level == UIEventLevel.SUCCESS and event.kind == UIEventKind.SESSION and local_id in event.message
        for event in ctx.ui_bus._history
    )


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
        event.level == UIEventLevel.WARNING
        and event.kind == UIEventKind.SESSION
        and "belongs to fingerprint 'remote:abc'" in event.message
        for event in ctx.ui_bus._history
    )
