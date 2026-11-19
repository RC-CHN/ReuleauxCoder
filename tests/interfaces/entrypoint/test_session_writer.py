import json
import multiprocessing
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore, SessionWriterBusy
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies, AppOptions
from reuleauxcoder.interfaces.entrypoint.session_lifecycle import restore_session


def _open_agent(directory, requested=None, *, latest=False):
    config = Config(api_key="test", session_dir=str(directory))
    agent = Agent(SimpleNamespace(model="test"), tools=[], config=config)
    bus = UIEventBus()
    restore_session(
        AppOptions(resume_session_id=requested, auto_resume_latest=latest),
        AppDependencies(), config, agent, bus,
    )
    return agent, config, bus


def _own_session(directory, session_id, ready):
    agent, _, _ = _open_agent(directory, session_id)
    try:
        agent._append_message({"role": "user", "content": "first writer"}, source="user_input")
        agent.persist_runtime_snapshot()
        ready.send(True)
        ready.recv()
    finally:
        agent.unbind_session_persistence()
        ready.close()


@pytest.mark.parametrize("corrupt_source", ["manifest", "ledger"])
def test_switch_with_invalid_goal_preserves_current_session(tmp_path, corrupt_source):
    store = SessionStore(tmp_path)
    target = store.save([{"role": "user", "content": "target"}], "test")
    events = store.get_session_events_path(target)
    if corrupt_source == "manifest":
        manifest = events.parent / "manifest.json"
        data = json.loads(manifest.read_bytes())
        data["runtime_state"]["goal"] = {"unknown": "field"}
        manifest.write_text(json.dumps(data), encoding="utf-8")
    else:
        rows = [json.loads(line) for line in events.read_bytes().splitlines()]
        event = dict(
            rows[-1],
            seq=rows[-1]["seq"] + 1,
            event_id="bad-goal",
            kind="goal_changed",
            payload={"goal": {"id": "g", "objective": "run", "time_used_seconds": "broken"}},
        )
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
    source_agent, config, bus = _open_agent(tmp_path)
    try:
        source = source_agent.current_session_id
        commands = CommandService(
            source_agent, config, bus, UIProfile("cli", "CLI", frozenset({UICapability.TEXT_INPUT})),
            create_builtin_action_registry(), sessions_dir=tmp_path,
        )
        source_agent._append_message({"role": "user", "content": "source"}, source="user_input")
        commands.submit(f"/session {target}")
        assert any("goal_validate" in event.message for event in bus.history_snapshot())
        assert source_agent.current_session_id == source
        assert [m["content"] for m in source_agent.messages] == ["source"]
        with pytest.raises(SessionWriterBusy):
            _open_agent(tmp_path, source)
        source_agent._append_message({"role": "user", "content": "continued"}, source="user_input")
        commands.submit("/save")
        assert [m["content"] for m in store.load(source).messages] == ["source", "continued"]
    finally:
        source_agent.unbind_session_persistence()


@pytest.mark.parametrize("crash", [False, True], ids=["normal-exit", "process-killed"])
def test_only_one_writer_can_resume_until_owner_exits(tmp_path, crash, monkeypatch):
    store = SessionStore(tmp_path)
    target = store.save([{"role": "user", "content": "initial"}], "test")
    spawn = multiprocessing.get_context("spawn")
    parent, child = spawn.Pipe()
    first = spawn.Process(target=_own_session, args=(tmp_path, target, child))
    first.start()
    child.close()
    second = None
    try:
        assert parent.poll(15) and parent.recv()
        with pytest.raises(SessionWriterBusy, match="Another rcoder process"):
            _open_agent(tmp_path, target)
        second, config, bus = _open_agent(tmp_path, latest=True)
        own_id = second.current_session_id
        assert own_id != target
        assert any("open in another rcoder" in e.message for e in bus.history_snapshot())
        second._append_message({"role": "user", "content": "independent"}, source="user_input")
        commands = CommandService(
            second, config, bus, UIProfile("cli", "CLI", frozenset({UICapability.TEXT_INPUT})),
            create_builtin_action_registry(), sessions_dir=tmp_path,
        )
        commands.submit(f"/session {target}")
        assert second.current_session_id == own_id
        with monkeypatch.context() as patch:
            patch.setattr(SessionStore, "generate_session_id", staticmethod(lambda: target))
            with pytest.raises(SessionWriterBusy):
                commands.submit("/new")
        assert second.current_session_id == own_id
        assert [message["content"] for message in second.messages] == ["independent"]
        with pytest.raises(SessionWriterBusy):
            _open_agent(tmp_path, own_id)
        if crash:
            first.terminate()
        else:
            parent.send("close")
        first.join(15)
        assert not first.is_alive()
        if not crash:
            assert first.exitcode == 0
        commands.submit(f"/session {target}")
        assert second.current_session_id == target
        second._append_message({"role": "user", "content": "next writer"}, source="user_input")
        second.unbind_session_persistence()
        loaded = store.load(target)
        assert [m["content"] for m in loaded.messages] == ["initial", "first writer", "next writer"]
        assert not any(issue.phase == "history_decode" for issue in loaded.restore_issues)
        assert [m["content"] for m in store.load(own_id).messages] == ["independent"]
    finally:
        if first.is_alive():
            first.terminate()
            first.join(15)
        parent.close()
        if second is not None:
            second.unbind_session_persistence()
