from __future__ import annotations

import shlex
import sys
import threading
import time

import pytest

from reuleauxcoder.domain.process import (
    MAX_PROCESS_INPUT_BYTES,
    MAX_PROCESS_SESSION_INPUT_BYTES,
    ProcessCapacityError,
    ProcessCursor,
    ProcessHandle,
    ProcessSessionNotFound,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamMode,
)
from reuleauxcoder.domain.process_manager import (
    ProcessEventKind,
    ProcessManager,
    _SensitiveOutputFilter,
)
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


def test_hidden_output_filter_redacts_values_split_across_chunks() -> None:
    values = ["cross-chunk-secret"]
    output_filter = _SensitiveOutputFilter(values)

    assert output_filter.apply("before cross-", final=False) == "before "
    assert (
        output_filter.apply("chunk-secret after", final=False)
        == "[hidden input redacted] after"
    )


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
        self.shutdown_calls = 0
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
        self.shutdown_calls += 1
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
    assert port.shutdown_calls == 1


class _ImmediateExitPort(_UnknownPort):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.state = ProcessState.EXITED

    def start(self, command, **_kwargs):
        del command
        return ProcessHandle(self.session_id, ProcessStreamMode.PIPE)

    def poll(self, session_id, *, cursor=None, wait_ms=0):
        del wait_ms
        return ProcessSnapshot(
            session_id=session_id,
            state=ProcessState.EXITED,
            stream_mode=ProcessStreamMode.PIPE,
            backend="local",
            cursor=cursor or ProcessCursor(),
            exit_code=0,
            started_at=time.time(),
            finished_at=time.time(),
            runtime_timeout_seconds=60,
        )


def _start_immediate_exit(
    manager: ProcessManager,
    port: _ImmediateExitPort,
) -> ProcessHandle:
    handle = manager.start(
        port,  # type: ignore[arg-type]
        "finished-command",
        cwd=".",
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    entry = manager._entries[handle.session_id]
    assert entry.watcher is not None
    entry.watcher.join(timeout=2)
    return handle


def test_capacity_does_not_discard_fresh_unobserved_terminal_result() -> None:
    manager = ProcessManager(
        max_sessions=1,
        observed_retention_seconds=0,
        terminal_ttl_seconds=600,
    )
    _start_immediate_exit(manager, _ImmediateExitPort("first-terminal"))

    with pytest.raises(ProcessCapacityError, match="capacity reached"):
        manager.start(
            _ImmediateExitPort("second-terminal"),  # type: ignore[arg-type]
            "second-command",
            cwd=".",
            runtime_timeout=60,
            tty=False,
            owner_agent_id="agent",
            owner_session_id="session",
            session_generation=0,
            origin_turn_id="turn",
        )

    assert "first-terminal" in manager._entries
    manager.shutdown()


def test_observed_retention_starts_when_result_is_observed() -> None:
    manager = ProcessManager(
        max_sessions=1,
        observed_retention_seconds=30,
        terminal_ttl_seconds=600,
    )
    handle = _start_immediate_exit(
        manager,
        _ImmediateExitPort("observed-terminal"),
    )
    entry = manager._entries[handle.session_id]
    entry.terminal_at = time.monotonic() - 300

    manager.poll(
        handle.session_id,
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
    )

    views = manager.list(
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        include_observed=True,
    )
    assert [view.session_id for view in views] == [handle.session_id]
    manager.shutdown()


def test_manager_shutdown_barrier_covers_inflight_start() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingPort(_UnknownPort):
        def start(self, command, **_kwargs):
            del command
            entered.set()
            assert release.wait(2)
            self.state = ProcessState.RUNNING
            return ProcessHandle("inflight-session", ProcessStreamMode.PIPE)

    port = _BlockingPort()
    manager = ProcessManager()
    start_errors = []
    reports = []

    def start() -> None:
        try:
            manager.start(
                port,  # type: ignore[arg-type]
                "slow-start",
                cwd=".",
                runtime_timeout=60,
                tty=False,
                owner_agent_id="agent",
                owner_session_id="session",
                session_generation=0,
                origin_turn_id="turn",
            )
        except BaseException as error:
            start_errors.append(error)

    starter = threading.Thread(target=start)
    starter.start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: reports.append(manager.shutdown()))
    closer.start()
    release.set()
    starter.join(timeout=5)
    closer.join(timeout=5)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert len(start_errors) == 1
    assert "shutting down" in str(start_errors[0])
    assert reports[0].reap_timeouts == 0
    assert port.terminate_calls == 1
    assert port.shutdown_calls == 1


def test_process_input_is_serialized_and_bounded_per_session() -> None:
    class _WritingPort(_UnknownPort):
        def __init__(self) -> None:
            super().__init__()
            self.state = ProcessState.RUNNING
            self.active_write = False
            self.overlapped = False
            self.writes = []
            self.write_state_lock = threading.Lock()

        def start(self, command, **_kwargs):
            del command
            return ProcessHandle("writing-session", ProcessStreamMode.PTY)

        def poll(self, session_id, *, cursor=None, wait_ms=0):
            del wait_ms
            return ProcessSnapshot(
                session_id=session_id,
                state=self.state,
                stream_mode=ProcessStreamMode.PTY,
                backend="local",
                cursor=cursor or ProcessCursor(),
                started_at=time.time(),
                runtime_timeout_seconds=60,
            )

        def write_input(self, session_id, data):
            del session_id
            with self.write_state_lock:
                if self.active_write:
                    self.overlapped = True
                self.active_write = True
            time.sleep(0.01)
            self.writes.append(data)
            with self.write_state_lock:
                self.active_write = False
            return len(data.encode("utf-8"))

    port = _WritingPort()
    manager = ProcessManager()
    handle = manager.start(
        port,  # type: ignore[arg-type]
        "interactive",
        cwd=".",
        runtime_timeout=60,
        tty=True,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    barrier = threading.Barrier(2)

    def write(consumer: str, chars: str) -> None:
        barrier.wait()
        manager.write(
            handle.session_id,
            chars,
            consumer=consumer,
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )

    writers = [
        threading.Thread(target=write, args=("first", "alpha")),
        threading.Thread(target=write, args=("second", "beta")),
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=2)

    assert port.overlapped is False
    assert sorted(port.writes) == ["alpha", "beta"]
    with pytest.raises(ProcessCapacityError, match="64 KiB"):
        manager.write(
            handle.session_id,
            "x" * (MAX_PROCESS_INPUT_BYTES + 1),
            consumer="oversized",
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
    remaining = MAX_PROCESS_SESSION_INPUT_BYTES - sum(
        len(value.encode("utf-8")) for value in port.writes
    )
    for index in range(0, remaining, MAX_PROCESS_INPUT_BYTES):
        chunk = "z" * min(MAX_PROCESS_INPUT_BYTES, remaining - index)
        manager.write(
            handle.session_id,
            chunk,
            consumer=f"fill-{index}",
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
    with pytest.raises(ProcessCapacityError, match="1 MiB"):
        manager.write(
            handle.session_id,
            "overflow",
            consumer="cumulative",
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
    manager.shutdown(grace_seconds=0)


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY integration")
def test_pty_session_tracks_terminal_resize(tmp_path) -> None:
    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        "stty size; read value; stty size",
        cwd=str(tmp_path),
        runtime_timeout=5,
        tty=True,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    initial = manager.poll(
        handle.session_id,
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        wait_ms=1000,
    )
    assert "24 80" in initial.stdout

    assert (
        manager.resize_tty_sessions(
            rows=40,
            columns=100,
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
        == 1
    )
    after_write = manager.write(
        handle.session_id,
        "\n",
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
    )
    output, _, state = _poll_until_terminal(manager, handle.session_id)

    assert state is ProcessState.EXITED
    assert "40 100" in after_write.stdout + output
    manager.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY integration")
def test_sensitive_pty_line_is_redacted_for_pollers_and_events(tmp_path) -> None:
    events = []
    manager = ProcessManager(event_sink=events.append)
    port = LocalProcessPort()
    secret = "not-for-model-context"
    handle = manager.start(
        port,
        _python_command(
            "print('ready', flush=True); "
            "value=input(); "
            "print('received:'+value, flush=True)"
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
    manager.poll(
        handle.session_id,
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        wait_ms=1000,
    )

    after_write = manager.write_sensitive_line(
        handle.session_id,
        secret,
        consumer="model",
        agent_id="agent",
        owner_session_id="session",
        session_generation=0,
    )
    output, _, state = _poll_until_terminal(manager, handle.session_id)
    observed = after_write.stdout + output
    deadline = time.monotonic() + 2
    while (
        not any(event.kind is ProcessEventKind.COMPLETED for event in events)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    event_output = "".join(
        event.snapshot.stdout + event.snapshot.stderr for event in events
    )

    assert state is ProcessState.EXITED
    assert any(event.kind is ProcessEventKind.COMPLETED for event in events)
    assert secret not in observed
    assert secret not in event_output
    assert "[hidden input redacted]" in observed
    manager.shutdown()
