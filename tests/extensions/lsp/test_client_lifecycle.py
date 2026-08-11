"""Cancellation and cleanup invariants for the stdio LSP client."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from reuleauxcoder.extensions.lsp.client import (
    LSP_STDERR_TAIL_BYTES,
    LspClient,
    LspClientError,
    LspRequestTimedOut,
)
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")


def _requestable_client(tmp_path: Path) -> LspClient:
    client = LspClient(LanguageId.PYTHON, tmp_path)
    client._process = MagicMock(stdin=object())
    return client


class _ImmediateStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _StubbornProcess:
    def __init__(
        self,
        *,
        exits_on_kill: bool,
        emit_stderr: bool = False,
        close_stderr_on_kill: bool = True,
    ) -> None:
        self.stdin = _ImmediateStdin()
        self.stderr = asyncio.StreamReader() if emit_stderr else None
        self.returncode: int | None = None
        self.exits_on_kill = exits_on_kill
        self.close_stderr_on_kill = close_stderr_on_kill
        self.wait_started = asyncio.Event()
        self.exited = asyncio.Event()
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        if self.stderr is not None:
            self.stderr.feed_data(b"after terminate|")

    def kill(self) -> None:
        self.killed = True
        if self.stderr is not None:
            self.stderr.feed_data(b"after kill")
        if self.exits_on_kill:
            self.returncode = -9
            self.exited.set()
            if self.stderr is not None and self.close_stderr_on_kill:
                self.stderr.feed_eof()

    async def wait(self) -> int:
        self.wait_started.set()
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode


def _fake_args(
    log_path: Path,
    *,
    initialize_behavior: str = "normal",
    shutdown_behavior: str = "normal",
    stderr_bytes: int = 0,
) -> list[str]:
    args = [
        "-u",
        str(FAKE_SERVER),
        "--mode",
        "push",
        "--log",
        str(log_path),
        "--initialize-behavior",
        initialize_behavior,
        "--shutdown-behavior",
        shutdown_behavior,
    ]
    if stderr_bytes:
        args.extend(("--stderr-bytes", str(stderr_bytes)))
    return args


def _events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _server_pid(log_path: Path) -> int:
    return int(
        next(
            event["pid"]
            for event in _events(log_path)
            if event["method"] == "server_started"
        )
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _assert_pid_exits(pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.01)
    assert not _pid_alive(pid)


def test_write_failure_removes_pending_request(tmp_path: Path) -> None:
    client = _requestable_client(tmp_path)
    client._write_message = AsyncMock(side_effect=BrokenPipeError("closed"))

    with pytest.raises(BrokenPipeError, match="closed"):
        asyncio.run(client._send_request("test/write", {}, timeout=1.0))

    assert client._pending == {}


def test_timeout_removes_pending_request(tmp_path: Path) -> None:
    client = _requestable_client(tmp_path)
    client._write_message = AsyncMock()

    with pytest.raises(LspRequestTimedOut, match="timed out"):
        asyncio.run(client._send_request("test/timeout", {}, timeout=0.001))

    assert client._pending == {}


def test_coroutine_cancellation_removes_pending_and_ignores_late_response(
    tmp_path: Path,
) -> None:
    client = _requestable_client(tmp_path)
    client._write_message = AsyncMock()

    async def run() -> None:
        task = asyncio.create_task(
            client._send_request("test/cancel", {}, timeout=10.0)
        )
        await asyncio.sleep(0)
        assert len(client._pending) == 1
        request_id = next(iter(client._pending))

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client._pending == {}
        client._dispatch_message({"jsonrpc": "2.0", "id": request_id, "result": "late"})
        assert client._pending == {}

    asyncio.run(run())


def test_eof_failure_clears_all_pending_idempotently(tmp_path: Path) -> None:
    client = _requestable_client(tmp_path)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        second = loop.create_future()
        client._pending = {1: first, 2: second}

        client._fail_all_pending("stdout EOF")
        client._fail_all_pending("stdout EOF again")

        assert client._pending == {}
        assert isinstance(first.exception(), LspClientError)
        assert isinstance(second.exception(), LspClientError)

    asyncio.run(run())


def test_failed_transport_remains_alive_until_process_is_reaped(
    tmp_path: Path,
) -> None:
    client = _requestable_client(tmp_path)
    client._process.returncode = None
    client._transport_failed = True

    assert client.is_alive
    assert not client.is_usable


def test_abort_force_closes_real_stdio_process_idempotently(tmp_path: Path) -> None:
    log_path = tmp_path / "abort.jsonl"
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> int:
        await client.spawn(sys.executable, _fake_args(log_path))
        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            not any(event["method"] == "server_started" for event in _events(log_path))
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        pid = _server_pid(log_path)
        assert _pid_alive(pid)

        await client.abort()
        await client.abort()

        assert client._process is None
        assert client._reader_task is None
        assert client._process_wait_task is None
        assert client._pending == {}
        return pid

    pid = asyncio.run(run())
    _assert_pid_exits(pid)


def test_large_stderr_is_drained_and_retains_exact_bounded_suffix(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "large-stderr.jsonl"
    stderr_bytes = 4 * 1024 * 1024 + 17
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> asyncio.Task[None]:
        await client.spawn(
            sys.executable,
            _fake_args(log_path, stderr_bytes=stderr_bytes),
        )
        stderr_task = client._stderr_task
        assert stderr_task is not None
        await asyncio.wait_for(client.initialize(), timeout=2.0)
        await client.shutdown(deadline_at=time.monotonic() + 1.0)
        await client.abort()
        return stderr_task

    stderr_task = asyncio.run(run())

    snapshot = client.stderr_snapshot
    suffix = b"\nFAKE_STDERR_END\n"
    expected_tail = b"x" * (LSP_STDERR_TAIL_BYTES - len(suffix)) + suffix
    assert snapshot.total_bytes == stderr_bytes
    assert snapshot.truncated is True
    assert snapshot.text.encode("utf-8") == expected_tail
    assert snapshot.read_error is None
    assert client._stderr_task is None
    assert stderr_task.done()
    assert not stderr_task.cancelled()


def test_stderr_snapshot_decodes_split_utf8_only_after_eof(tmp_path: Path) -> None:
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> None:
        stderr = asyncio.StreamReader()
        process = MagicMock(stderr=stderr)
        task = asyncio.create_task(
            client._drain_stderr(process, client._stderr_capture)
        )
        await asyncio.sleep(0)
        stderr.feed_data(b"no-newline:\xe4")
        await asyncio.sleep(0)
        stderr.feed_data(b"\xb8\xad")
        stderr.feed_eof()
        await task

    asyncio.run(run())

    assert client.stderr_snapshot.text == "no-newline:中"
    assert client.stderr_snapshot.total_bytes == len("no-newline:中".encode())
    assert client.stderr_snapshot.read_error is None


def test_abort_drains_through_kill_then_bounds_missing_stderr_eof(
    tmp_path: Path,
) -> None:
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> tuple[_StubbornProcess, asyncio.Task[None], float]:
        process = _StubbornProcess(
            exits_on_kill=True,
            emit_stderr=True,
            close_stderr_on_kill=False,
        )
        client._process = process  # type: ignore[assignment]
        client._stderr_task = asyncio.create_task(
            client._drain_stderr(process, client._stderr_capture)  # type: ignore[arg-type]
        )
        stderr_task = client._stderr_task
        await asyncio.sleep(0)
        started_at = time.monotonic()
        await client.abort(deadline_at=started_at + 0.35)
        await client.abort()
        return process, stderr_task, time.monotonic() - started_at

    process, stderr_task, elapsed = asyncio.run(run())

    assert elapsed < 0.55
    assert process.terminated
    assert process.killed
    assert client.stderr_snapshot.text == "after terminate|after kill"
    assert client.stderr_snapshot.read_error == (
        "stderr reader did not reach EOF before cleanup deadline"
    )
    assert client._stderr_task is None
    assert stderr_task.done()
    assert stderr_task.cancelled()


def test_stderr_reader_error_is_retained_without_crashing_transport_task(
    tmp_path: Path,
) -> None:
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )

    async def run() -> None:
        stderr = AsyncMock()
        stderr.read.side_effect = OSError("deterministic read failure")
        process = MagicMock(stderr=stderr, returncode=None)
        client._process = process
        pending = asyncio.get_running_loop().create_future()
        client._pending[1] = pending

        await client._drain_stderr(process, client._stderr_capture)

        assert isinstance(pending.exception(), LspClientError)
        process.kill.assert_called_once_with()

    asyncio.run(run())

    assert client.stderr_snapshot.read_error == ("OSError: deterministic read failure")
    assert not client.is_usable
    assert [(reason, returncode) for _, reason, returncode in callbacks] == [
        ("stderr reader failed: OSError", None)
    ]


def test_stderr_force_kill_failure_is_retained_without_sensitive_detail(
    tmp_path: Path,
) -> None:
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> None:
        stderr = AsyncMock()
        stderr.read.side_effect = OSError("reader failed")
        process = MagicMock(stderr=stderr, returncode=None)
        process.kill.side_effect = PermissionError("credential=must-not-leak")
        client._process = process

        await client._drain_stderr(process, client._stderr_capture)

    asyncio.run(run())

    assert client.stderr_snapshot.cleanup_error == (
        "stderr_force_kill_failed: PermissionError"
    )
    assert "must-not-leak" not in repr(client.stderr_snapshot)


def test_unexpected_exit_bounds_stderr_without_eof_when_idle(tmp_path: Path) -> None:
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )

    async def run() -> asyncio.Task[None]:
        process = _StubbornProcess(
            exits_on_kill=True,
            emit_stderr=True,
            close_stderr_on_kill=False,
        )
        assert process.stderr is not None
        process.stderr.feed_data(b"final idle marker")
        process.returncode = 23
        client._process = process  # type: ignore[assignment]
        stderr_task = asyncio.create_task(
            client._drain_stderr(process, client._stderr_capture)  # type: ignore[arg-type]
        )
        client._stderr_task = stderr_task

        watcher = asyncio.create_task(client._watch_process_exit(process))  # type: ignore[arg-type]
        await asyncio.wait_for(watcher, timeout=0.6)
        return stderr_task

    stderr_task = asyncio.run(run())

    assert [(reason, returncode) for _, reason, returncode in callbacks] == [
        ("process exited", 23)
    ]
    assert client.stderr_snapshot.text == "final idle marker"
    assert client.stderr_snapshot.read_error == (
        "stderr reader did not reach EOF before cleanup deadline"
    )
    assert client._stderr_task is None
    assert stderr_task.done()
    assert stderr_task.cancelled()


def test_abort_finishes_force_kill_before_propagating_cancellation(
    tmp_path: Path,
) -> None:
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> _StubbornProcess:
        process = _StubbornProcess(exits_on_kill=True)
        client._process = process  # type: ignore[assignment]
        client._initialized = True
        abort = asyncio.create_task(client.abort(deadline_at=time.monotonic() + 0.35))
        await asyncio.wait_for(process.wait_started.wait(), timeout=0.2)

        abort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(abort, timeout=0.6)
        return process

    process = asyncio.run(run())

    assert process.terminated
    assert process.killed
    assert process.stdin.closed
    assert client._process is None
    assert not client.is_initialized


def test_abort_preserves_unreaped_process_as_unusable(tmp_path: Path) -> None:
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> _StubbornProcess:
        process = _StubbornProcess(exits_on_kill=False)
        client._process = process  # type: ignore[assignment]
        client._initialized = True
        await client.abort(deadline_at=time.monotonic())
        return process

    process = asyncio.run(run())

    assert process.terminated
    assert process.killed
    assert process.stdin.closed
    assert client._process is process
    assert client.is_alive
    assert not client.is_usable
    assert not client.is_initialized


def test_failed_initialize_aborts_unregistered_process(tmp_path: Path) -> None:
    log_path = tmp_path / "init-error.jsonl"
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(
        LspConfig(
            server_overrides={
                "python": LspServerOverride(
                    language="python",
                    cmd=sys.executable,
                    args=_fake_args(log_path, initialize_behavior="error"),
                )
            }
        ),
        workspace_cwd=tmp_path,
    )
    manager._availability[LanguageId.PYTHON] = True

    result = asyncio.run(manager._spawn_async(LanguageId.PYTHON, source))

    assert result is None
    assert manager._transports == {}
    assert manager._re_spawn_counts[(LanguageId.PYTHON, tmp_path.resolve())] == 1
    _assert_pid_exits(_server_pid(log_path))


def test_cancelled_initialize_aborts_unregistered_process(tmp_path: Path) -> None:
    log_path = tmp_path / "init-cancel.jsonl"
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(
        LspConfig(
            server_overrides={
                "python": LspServerOverride(
                    language="python",
                    cmd=sys.executable,
                    args=_fake_args(log_path, initialize_behavior="hang"),
                )
            }
        ),
        workspace_cwd=tmp_path,
    )
    manager._availability[LanguageId.PYTHON] = True

    async def run() -> None:
        task = asyncio.create_task(manager._spawn_async(LanguageId.PYTHON, source))
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if any(
                event["method"] == "initialize_hanging" for event in _events(log_path)
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("fake server never entered initialize hang")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert manager._transports == {}
    _assert_pid_exits(_server_pid(log_path))


def test_shutdown_hang_is_force_closed_within_total_deadline(tmp_path: Path) -> None:
    log_path = tmp_path / "shutdown-hang.jsonl"
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> tuple[int, float]:
        await client.spawn(
            sys.executable,
            _fake_args(log_path, shutdown_behavior="hang"),
        )
        await client.initialize()
        pid = _server_pid(log_path)

        started_at = time.monotonic()
        await client.shutdown(deadline_at=started_at + 0.5)
        elapsed = time.monotonic() - started_at

        assert client._process is None
        assert client._reader_task is None
        assert client._process_wait_task is None
        return pid, elapsed

    pid, elapsed = asyncio.run(run())

    assert elapsed < 0.75
    assert any(event["method"] == "shutdown_hanging" for event in _events(log_path))
    _assert_pid_exits(pid)


def test_unexpected_exit_callback_is_once_per_process(tmp_path: Path) -> None:
    log_path = tmp_path / "unexpected-exit.jsonl"
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )

    async def run() -> int:
        await client.spawn(sys.executable, _fake_args(log_path))
        await client.initialize()
        process = client._process
        assert process is not None
        process.terminate()
        watcher = client._process_wait_task
        assert watcher is not None
        await asyncio.wait_for(watcher, timeout=1.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not client.is_alive
        assert len(callbacks) == 1
        assert callbacks[0][0] is client
        assert callbacks[0][1]
        await client.abort()
        assert len(callbacks) == 1
        return process.pid

    pid = asyncio.run(run())
    _assert_pid_exits(pid)


def test_graceful_shutdown_does_not_report_unexpected_exit(tmp_path: Path) -> None:
    log_path = tmp_path / "expected-exit.jsonl"
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )

    async def run() -> int:
        await client.spawn(sys.executable, _fake_args(log_path))
        await client.initialize()
        process = client._process
        assert process is not None
        pid = process.pid
        await client.shutdown(deadline_at=time.monotonic() + 1.0)
        assert callbacks == []
        assert client._process_wait_task is None
        return pid

    pid = asyncio.run(run())
    _assert_pid_exits(pid)
