from __future__ import annotations

import shlex
import sys
import threading
import time

import pytest

from reuleauxcoder.domain.process import (
    ProcessCursor,
    ProcessHandle,
    ProcessSessionNotFound,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamMode,
)
from reuleauxcoder.domain.process_manager import ProcessEventKind, ProcessManager
from reuleauxcoder.infrastructure.process.local import LocalProcessPort


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _poll_until_terminal(
    manager: ProcessManager,
    session_id: str,
    *,
    consumer: str = "model",
) -> tuple[str, str, ProcessState]:
    stdout: list[str] = []
    stderr: list[str] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = manager.poll(
            session_id,
            consumer=consumer,
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
            wait_ms=100,
        )
        stdout.append(snapshot.stdout)
        stderr.append(snapshot.stderr)
        if snapshot.state is not ProcessState.RUNNING:
            return "".join(stdout), "".join(stderr), snapshot.state
    raise AssertionError("process did not exit")


def test_manager_keeps_independent_consumer_cursors(tmp_path) -> None:
    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command("print('hello', flush=True)"),
        cwd=str(tmp_path),
        runtime_timeout=5,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)

    model_output, _, state = _poll_until_terminal(manager, handle.session_id)
    ui_output, _, _ = _poll_until_terminal(
        manager, handle.session_id, consumer="ui"
    )

    assert state is ProcessState.EXITED
    assert model_output == "hello\n"
    assert ui_output == "hello\n"
    manager.shutdown()


def test_manager_rejects_stale_generation_but_can_rebind(tmp_path) -> None:
    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)

    with pytest.raises(ProcessSessionNotFound):
        manager.get_view(
            handle.session_id,
            agent_id="agent",
            owner_session_id="session",
            session_generation=1,
        )

    assert (
        manager.rebind_generation(
            owner_session_id="session",
            previous_generation=0,
            next_generation=1,
        )
        == 1
    )
    view = manager.get_view(
        handle.session_id,
        agent_id="agent",
        owner_session_id="session",
        session_generation=1,
    )
    assert view.state is ProcessState.RUNNING
    manager.shutdown(grace_seconds=0)


def test_unpublished_cancel_is_cleaned_without_completion_event(tmp_path) -> None:
    events = []
    manager = ProcessManager(event_sink=events.append)
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )

    manager.abandon(handle.session_id, reason="cancelled")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            manager.get_view(
                handle.session_id,
                agent_id="agent",
                owner_session_id="session",
                session_generation=0,
            )
        except ProcessSessionNotFound:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("abandoned session was not removed")

    assert all(event.kind is not ProcessEventKind.COMPLETED for event in events)
    manager.shutdown()


class _UnknownPort:
    backend_name = "remote"

    def __init__(self) -> None:
        self.state = ProcessState.UNKNOWN
        self.terminate_calls = 0
        self._lock = threading.Lock()

    def start(self, command, **_kwargs):
        del command
        return ProcessHandle("unknown-session", ProcessStreamMode.PIPE)

    def poll(self, session_id, *, cursor=None, wait_ms=0):
        if wait_ms:
            time.sleep(min(wait_ms / 1000, 0.01))
        with self._lock:
            state = self.state
        return ProcessSnapshot(
            session_id=session_id,
            state=state,
            stream_mode=ProcessStreamMode.PIPE,
            backend="remote",
            cursor=cursor or ProcessCursor(),
            started_at=time.time(),
            runtime_timeout_seconds=60,
        )

    def terminate(self, session_id, *, reason="terminated"):
        del reason
        with self._lock:
            self.terminate_calls += 1
            self.state = ProcessState.EXITED
        return self.poll(session_id)

    def interrupt(self, session_id):
        return self.poll(session_id)

    def write_input(self, session_id, data):
        del session_id
        return len(data)

    def release(self, session_id):
        del session_id

    def shutdown(self, *, grace_seconds=0.5):
        del grace_seconds
        return ProcessShutdownReport()


def test_unknown_is_unresolved_not_a_synthetic_completion() -> None:
    events = []
    port = _UnknownPort()
    manager = ProcessManager(event_sink=events.append)
    handle = manager.start(
        port,  # type: ignore[arg-type]
        "ambiguous-command",
        cwd=".",
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    time.sleep(0.03)

    assert manager.active_count(owner_session_id="session") == 1
    assert manager.list(
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
    )[0].state is ProcessState.UNKNOWN
    assert all(event.kind is not ProcessEventKind.COMPLETED for event in events)

    assert (
        manager.stop_all(
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
        == 1
    )
    manager.shutdown()
    assert port.terminate_calls == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY integration")
def test_pty_session_accepts_incremental_input(tmp_path) -> None:
    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command(
            "print('ready', flush=True); value=input(); print('got:'+value, flush=True)"
        ),
        cwd=str(tmp_path),
        runtime_timeout=5,
        tty=True,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)

    ready = manager.poll(
        handle.session_id,
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        wait_ms=1000,
    )
    assert "ready" in ready.stdout

    after_write = manager.write(
        handle.session_id,
        "answer\n",
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
    )
    output, _, state = _poll_until_terminal(manager, handle.session_id)

    assert state is ProcessState.EXITED
    assert "got:answer" in after_write.stdout + output
    manager.shutdown()
