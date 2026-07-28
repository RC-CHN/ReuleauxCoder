import os
import shlex
import signal
import sys
import threading
import time

import pytest

from reuleauxcoder.domain.process import ProcessCursor, ProcessState
from reuleauxcoder.infrastructure.process.local import LocalProcessPort
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def test_timeout_preserves_and_streams_partial_output(tmp_path) -> None:
    chunks = []
    command = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
        "import time\n"
        "print('line-0', flush=True)\n"
        "print('line-1', flush=True)\n"
        "time.sleep(30)\n"
    )

    result = LocalProcessPort().run(
        command,
        cwd=str(tmp_path),
        timeout=1,
        stream_handler=chunks.append,
    )

    assert result.timed_out is True
    assert "line-0" in result.stdout
    assert "line-1" in result.stdout
    assert "".join(chunk.data for chunk in chunks) == result.stdout


def test_cancellation_returns_promptly_without_reaping(tmp_path) -> None:
    cancellation = threading.Event()
    shell = get_platform_info().get_preferred_shell()
    if shell is ShellType.BASH:
        sleeper = "sleep 30"
    elif shell in (ShellType.POWERSHELL, ShellType.POWERSHELL_CORE):
        sleeper = "Start-Sleep -Seconds 30"
    else:
        sleeper = "timeout /t 30 /nobreak"

    def cancel_soon() -> None:
        time.sleep(0.2)
        cancellation.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    result = LocalProcessPort().run(
        sleeper,
        cwd=str(tmp_path),
        timeout=60,
        cancellation_event=cancellation,
    )
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    # Termination signals fire and the caller unwinds without waiting for the
    # process group to actually die (reaped asynchronously).
    assert elapsed < 1.5


def test_invalid_utf8_is_replaced_and_reported_as_a_fact(tmp_path) -> None:
    port = LocalProcessPort()
    command = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
        "import sys; sys.stdout.buffer.write(b'valid\\xffbytes')"
    )
    handle = port.start(
        command,
        cwd=str(tmp_path),
        runtime_timeout=5,
    )
    cursor = ProcessCursor()
    output = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = port.poll(handle.session_id, cursor=cursor, wait_ms=100)
        cursor = snapshot.cursor
        output.append(snapshot.stdout)
        if snapshot.state is ProcessState.EXITED:
            break
    else:
        raise AssertionError("process did not exit")

    assert "".join(output) == "valid\ufffdbytes"
    assert snapshot.output_decode_replaced is True
    port.release(handle.session_id)


def test_output_truncation_fact_never_regresses(tmp_path) -> None:
    port = LocalProcessPort(
        retained_bytes_per_stream=16 * 1024,
        poll_bytes_per_stream=1024,
    )
    handle = port.start(
        _python_command("import sys; sys.stdout.write('x' * 5000)"),
        cwd=str(tmp_path),
        runtime_timeout=5,
    )
    deadline = time.monotonic() + 3
    cursor = ProcessCursor()
    snapshots = []
    while time.monotonic() < deadline:
        snapshot = port.poll(handle.session_id, cursor=cursor, wait_ms=100)
        snapshots.append(snapshot)
        cursor = snapshot.cursor
        if snapshot.state is ProcessState.EXITED and not snapshot.stdout:
            break
    else:
        raise AssertionError("process output did not drain")

    first_truncated = next(
        index
        for index, snapshot in enumerate(snapshots)
        if snapshot.output_truncated
    )
    assert all(
        snapshot.output_truncated for snapshot in snapshots[first_truncated:]
    )
    assert sum(len(snapshot.stdout) for snapshot in snapshots) == 5000
    port.release(handle.session_id)


def test_concurrent_idempotent_start_spawns_once(tmp_path) -> None:
    port = LocalProcessPort()
    output_path = tmp_path / "starts.txt"
    command = _python_command(
        "from pathlib import Path; "
        f"path=Path({str(output_path)!r}); "
        "path.write_text((path.read_text() if path.exists() else '') + 'start\\n')"
    )
    barrier = threading.Barrier(6)
    handles = []
    errors = []

    def start() -> None:
        try:
            barrier.wait()
            handles.append(
                port.start(
                    command,
                    cwd=str(tmp_path),
                    runtime_timeout=5,
                    idempotency_key="same-intent",
                )
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(handles) == 6
    assert len({handle.session_id for handle in handles}) == 1
    session_id = handles[0].session_id
    deadline = time.monotonic() + 3
    while port.poll(session_id, wait_ms=100).state is ProcessState.RUNNING:
        assert time.monotonic() < deadline
    assert output_path.read_text() == "start\n"
    port.release(session_id)


def test_terminal_session_remains_queryable_until_release(tmp_path) -> None:
    port = LocalProcessPort()
    first = port.start(
        _python_command("print('first')"),
        cwd=str(tmp_path),
        runtime_timeout=5,
    )
    deadline = time.monotonic() + 3
    first_snapshot = port.poll(first.session_id, wait_ms=100)
    while first_snapshot.state is ProcessState.RUNNING:
        assert time.monotonic() < deadline
        first_snapshot = port.poll(first.session_id, wait_ms=100)

    second = port.start(
        _python_command("print('second')"),
        cwd=str(tmp_path),
        runtime_timeout=5,
    )

    retained = port.poll(first.session_id)
    assert retained.state is ProcessState.EXITED
    assert "first" in retained.stdout
    port.release(first.session_id)
    port.terminate(second.session_id)
    port.shutdown(grace_seconds=0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group assertion")
def test_root_exit_bounds_descendant_pipe_drain(tmp_path) -> None:
    port = LocalProcessPort()
    started = time.monotonic()
    handle = port.start(
        "sleep 30 & printf done",
        cwd=str(tmp_path),
        runtime_timeout=10,
    )
    deadline = time.monotonic() + 3
    snapshot = port.poll(handle.session_id, wait_ms=100)
    while snapshot.state is ProcessState.RUNNING:
        assert time.monotonic() < deadline
        snapshot = port.poll(
            handle.session_id,
            cursor=snapshot.cursor,
            wait_ms=100,
        )

    assert time.monotonic() - started < 2
    retained = port.poll(handle.session_id)
    assert retained.exit_code == 0
    assert retained.stdout == "done"
    port.release(handle.session_id)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group assertion")
def test_root_exit_reaps_descendant_with_redirected_streams(tmp_path) -> None:
    marker = tmp_path / "descendant-output"
    pid_file = tmp_path / "descendant-pid"
    command = (
        "(trap '' HUP TERM; while :; do printf x >> "
        f"{shlex.quote(str(marker))}; sleep 0.05; done) "
        f">/dev/null 2>&1 & printf '%s' \"$!\" > {shlex.quote(str(pid_file))}"
    )
    port = LocalProcessPort()
    handle = port.start(command, cwd=str(tmp_path), runtime_timeout=10)
    deadline = time.monotonic() + 3
    snapshot = port.poll(handle.session_id, wait_ms=100)
    while snapshot.state is ProcessState.RUNNING:
        assert time.monotonic() < deadline
        snapshot = port.poll(
            handle.session_id,
            cursor=snapshot.cursor,
            wait_ms=100,
        )

    descendant_pid = int(pid_file.read_text())
    try:
        time.sleep(0.15)
        first_size = marker.stat().st_size if marker.exists() else 0
        time.sleep(0.2)
        second_size = marker.stat().st_size if marker.exists() else 0
        assert second_size == first_size
    finally:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    port.release(handle.session_id)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal assertion")
def test_repeated_process_controls_are_idempotent(monkeypatch, tmp_path) -> None:
    port = LocalProcessPort()
    handle = port.start(
        "trap '' INT TERM; while :; do sleep 1; done",
        cwd=str(tmp_path),
        runtime_timeout=30,
    )
    entry = port._lookup(handle.session_id)
    original_killpg = os.killpg
    signals: list[int] = []

    def recording_killpg(process_group: int, selected_signal: int) -> None:
        signals.append(selected_signal)
        original_killpg(process_group, selected_signal)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    port.interrupt(handle.session_id)
    port.interrupt(handle.session_id)
    assert signals.count(signal.SIGINT) == 1

    baseline_threads = len(entry.control_threads)
    port.terminate(handle.session_id, reason="test_cleanup")
    port.terminate(handle.session_id, reason="test_cleanup")
    assert len(entry.control_threads) == baseline_threads + 1
    port.shutdown(grace_seconds=0)


def test_start_failure_after_spawn_is_reaped(monkeypatch, tmp_path) -> None:
    port = LocalProcessPort()
    spawned = []
    original_spawn = port._spawn_pipe

    def capture_spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    def fail_readers(_entry):
        raise RuntimeError("reader setup failed")

    monkeypatch.setattr(port, "_spawn_pipe", capture_spawn)
    monkeypatch.setattr(port, "_start_readers", fail_readers)

    with pytest.raises(RuntimeError, match="reader setup failed"):
        port.start(
            _python_command("import time; time.sleep(30)"),
            cwd=str(tmp_path),
            runtime_timeout=60,
        )

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert port.shutdown().total == 0


def test_shutdown_waits_for_and_reaps_inflight_start(monkeypatch, tmp_path) -> None:
    port = LocalProcessPort()
    entered_spawn = threading.Event()
    allow_spawn = threading.Event()
    spawned = []
    errors = []
    original_spawn = port._spawn_pipe

    def delayed_spawn(*args, **kwargs):
        entered_spawn.set()
        assert allow_spawn.wait(2)
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(port, "_spawn_pipe", delayed_spawn)

    starter = threading.Thread(
        target=lambda: _record_start_error(
            errors,
            port,
            _python_command("import time; time.sleep(30)"),
            str(tmp_path),
        )
    )
    starter.start()
    assert entered_spawn.wait(2)
    reports = []
    closer = threading.Thread(target=lambda: reports.append(port.shutdown()))
    closer.start()
    allow_spawn.set()
    starter.join(timeout=5)
    closer.join(timeout=5)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert len(errors) == 1
    assert "shutting down" in str(errors[0])
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert reports[0].reap_timeouts == 0


def _record_start_error(
    errors: list[BaseException],
    port: LocalProcessPort,
    command: str,
    cwd: str,
) -> None:
    try:
        port.start(command, cwd=cwd, runtime_timeout=60)
    except BaseException as error:
        errors.append(error)
