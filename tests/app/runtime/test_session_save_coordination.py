import json
import threading
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.runtime.session_state import bind_session_persistence, save_session_snapshot
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


@pytest.fixture
def session(tmp_path):
    config = Config(api_key="test", session_dir=tmp_path, session_auto_save=True)
    agent = Agent(SimpleNamespace(model="test"), tools=[], config=config)
    store = SessionStore(tmp_path)
    sid = store.generate_session_id()
    bind_session_persistence(config, agent, store, sid, fingerprint="local")
    agent._append_message({"role": "user", "content": "keep this"}, source="user_input")
    agent._session_persist_callback._delay = 60
    try:
        yield config, agent, store, sid
    finally:
        agent.unbind_session_persistence()


def assert_valid_exit(store, sid):
    rows = [json.loads(line) for line in (store.sessions_dir / sid / "events.jsonl").read_text().splitlines()]
    assert all(a["seq"] < b["seq"] for a, b in zip(rows, rows[1:]))
    assert len({row["event_id"] for row in rows}) == len(rows)
    loaded = store.load(sid)
    assert not loaded.restore_issues
    assert sum("[SESSION_EXIT]" in str(message.get("content")) for message in loaded.messages) == 1
    return loaded


def test_quit_and_late_live_events_keep_one_sequence_owner(session):
    config, agent, store, sid = session
    commands = CommandService(
        agent, config, UIEventBus(), UIProfile("cli", "CLI", frozenset(UICapability)),
        create_builtin_action_registry(), sessions_dir=store.sessions_dir,
    )
    assert commands.submit("/quit").control == "exit"
    agent.history_ledger.append("goal_changed", {"goal": None})
    agent.persist_runtime_snapshot()
    assert commands.save_exit() == sid
    loaded = assert_valid_exit(store, sid)
    assert loaded.history_events[-1].kind == "goal_changed"


def test_failed_exit_save_retries_without_allocating_another_exit(session, monkeypatch):
    config, agent, store, sid = session
    original = store.save
    monkeypatch.setattr(store, "save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        save_session_snapshot(config, agent, store, sid, is_exit=True)
    monkeypatch.setattr(store, "save", original)
    save_session_snapshot(config, agent, store, sid, is_exit=True)
    assert_valid_exit(store, sid)


def test_explicit_exit_cannot_be_overwritten_by_an_older_live_snapshot(session, monkeypatch):
    config, agent, store, sid = session
    entered, release, saving = threading.Event(), threading.Event(), threading.Event()
    original = store._atomic_write_json
    errors = []

    def write(path, payload, **kwargs):
        if threading.current_thread().name == "live-snapshot" and kwargs["ref"] == "replay":
            entered.set()
            assert release.wait(3)
        return original(path, payload, **kwargs)

    def run(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    def save():
        saving.set()
        save_session_snapshot(config, agent, SessionStore(store.sessions_dir), sid, is_exit=True)

    monkeypatch.setattr(store, "_atomic_write_json", write)
    live = threading.Thread(target=run, args=(agent.persist_runtime_snapshot,), name="live-snapshot", daemon=True)
    explicit = threading.Thread(target=run, args=(save,), daemon=True)
    live.start()
    try:
        assert entered.wait(3)
        explicit.start()
        assert saving.wait(3)
    finally:
        release.set()
    live.join(3)
    explicit.join(3)
    assert not live.is_alive() and not explicit.is_alive()
    assert not errors
    replay = json.loads((store.sessions_dir / sid / "replay.json").read_text())
    assert "[SESSION_EXIT]" in replay["items"][-1]["content"]
    assert_valid_exit(store, sid)
