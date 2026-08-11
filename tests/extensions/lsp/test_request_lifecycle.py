"""End-to-end deadlines for the synchronous LSP manager bridge."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reuleauxcoder.extensions.lsp.client import (
    LspClientError,
    LspRequestCancelled,
    LspRequestTimedOut,
)
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import LspManager, ToolRequest
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")


class _ControlledServer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.calls: list[str] = []
        self.request_timeouts: list[float] = []

    async def did_open(self, _path: Path, _content: str) -> None:
        return None

    async def did_change(self, _path: Path, _content: str) -> None:
        return None

    async def send_request(
        self,
        method: str,
        _params: dict[str, Any],
        *,
        timeout: float,
    ) -> str:
        self.calls.append(method)
        self.request_timeouts.append(timeout)
        if method != "slow":
            return method
        self.started.set()
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return method


class _SlowShutdownClient:
    def __init__(self) -> None:
        self.deadline_at: float | None = None
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return not self.closed

    @property
    def is_usable(self) -> bool:
        return self.is_alive

    async def shutdown(self, *, deadline_at: float) -> None:
        self.deadline_at = deadline_at
        await asyncio.sleep(0.2)
        self.closed = True

    async def abort(self, *, deadline_at: float) -> None:
        self.deadline_at = deadline_at
        self.closed = True


class _BrokenShutdownClient:
    is_alive = True
    is_usable = True

    async def shutdown(self, *, deadline_at: float) -> None:
        raise RuntimeError(f"still alive at {deadline_at}")

    async def abort(self, *, deadline_at: float) -> None:
        return None


class _UnusableLiveClient:
    def __init__(self) -> None:
        self.is_alive = True
        self.is_usable = False
        self.aborted = False

    async def shutdown(self, *, deadline_at: float) -> None:
        raise AssertionError(f"unusable transport was shut down at {deadline_at}")

    async def abort(self, *, deadline_at: float) -> None:
        self.aborted = True
        self.is_alive = False


class _CancelDuringSetFuture(concurrent.futures.Future[Any]):
    def set_exception(self, exception: BaseException) -> None:
        self.cancel()
        super().set_exception(exception)


def _manager_with_server(tmp_path: Path, server: _ControlledServer) -> LspManager:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def get_server(
        _language: LanguageId,
        _path: Path,
        *,
        transport_key=None,
    ):
        return server

    manager._get_or_create_server = get_server  # type: ignore[method-assign]
    return manager


def _fake_args(log_path: Path, *, initialize_behavior: str) -> list[str]:
    return [
        "-u",
        str(FAKE_SERVER),
        "--mode",
        "push",
        "--log",
        str(log_path),
        "--initialize-behavior",
        initialize_behavior,
    ]


def _events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.01)
    assert not _pid_alive(pid)


def test_queue_wait_consumes_deadline_without_executing_expired_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    server = _ControlledServer()
    manager = _manager_with_server(tmp_path, server)
    first_result: list[str] = []

    def run_first() -> None:
        first_result.append(manager.send_request_sync(source, "slow", {}, timeout=2.0))

    thread = threading.Thread(target=run_first)
    thread.start()
    assert server.started.wait(timeout=1.0)

    started_at = time.monotonic()
    with pytest.raises(LspRequestTimedOut, match="total"):
        manager.send_request_sync(source, "fast", {}, timeout=0.05)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert server.calls == ["slow"]
    server.release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert first_result == ["slow"]
    manager.shutdown_all()


def test_inflight_timeout_cancels_operation_and_worker_serves_next_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    server = _ControlledServer()
    manager = _manager_with_server(tmp_path, server)

    with pytest.raises(LspRequestTimedOut, match="total"):
        manager.send_request_sync(source, "slow", {}, timeout=0.5)

    assert server.started.is_set()
    assert server.cancelled.wait(timeout=1.0)
    assert manager.send_request_sync(source, "fast", {}, timeout=1.0) == "fast"
    assert server.calls == ["slow", "fast"]
    manager.shutdown_all()


def test_inflight_caller_cancellation_is_prompt_and_worker_recovers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    server = _ControlledServer()
    manager = _manager_with_server(tmp_path, server)
    cancellation = threading.Event()

    def cancel_after_start() -> None:
        assert server.started.wait(timeout=1.0)
        cancellation.set()

    canceller = threading.Thread(target=cancel_after_start)
    canceller.start()
    started_at = time.monotonic()
    with pytest.raises(LspRequestCancelled, match="cancelled"):
        manager.send_request_sync(
            source,
            "slow",
            {},
            timeout=5.0,
            cancellation=cancellation,
        )
    elapsed = time.monotonic() - started_at

    canceller.join(timeout=1.0)
    assert elapsed < 1.0
    assert server.cancelled.wait(timeout=1.0)
    assert manager.send_request_sync(source, "fast", {}, timeout=1.0) == "fast"
    manager.shutdown_all()


def test_pre_cancelled_request_does_not_start_worker(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    cancellation = threading.Event()
    cancellation.set()
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    with pytest.raises(LspRequestCancelled, match="cancelled"):
        manager.send_request_sync(
            source,
            "never-started",
            {},
            cancellation=cancellation,
        )

    assert manager._worker_thread is None


def test_shutdown_permanently_rejects_new_work(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    assert manager.shutdown_all(timeout=0.5) is True

    with pytest.raises(LspClientError, match="shutting down"):
        manager.send_request_sync(source, "after-shutdown", {}, timeout=0.5)
    assert manager.enqueue_diagnostics(source) is None
    assert manager._worker_thread is None


def test_query_receives_only_remaining_end_to_end_budget(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    server = _ControlledServer()
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def delayed_start(
        _language: LanguageId,
        _path: Path,
        *,
        transport_key=None,
    ):
        await asyncio.sleep(0.08)
        return server

    manager._get_or_create_server = delayed_start  # type: ignore[method-assign]

    assert manager.send_request_sync(source, "fast", {}, timeout=0.4) == "fast"

    assert len(server.request_timeouts) == 1
    assert 0 < server.request_timeouts[0] < 0.36
    manager.shutdown_all()


def test_hung_initialize_is_cancelled_at_operation_deadline(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hanging_log = tmp_path / "hanging.jsonl"
    override = LspServerOverride(
        language="python",
        cmd=sys.executable,
        args=_fake_args(hanging_log, initialize_behavior="hang"),
    )
    manager = LspManager(
        LspConfig(server_overrides={"python": override}),
        workspace_cwd=tmp_path,
    )
    manager._availability[LanguageId.PYTHON] = True

    started_at = time.monotonic()
    with pytest.raises(LspRequestTimedOut, match="1s total"):
        manager.send_request_sync(source, "test/hung", {}, timeout=1.0)
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.5
    started = next(
        event for event in _events(hanging_log) if event["method"] == "server_started"
    )
    _wait_for_pid_exit(int(started["pid"]))

    healthy_log = tmp_path / "healthy.jsonl"
    override.args = _fake_args(healthy_log, initialize_behavior="normal")
    assert manager.send_request_sync(source, "test/healthy", {}, timeout=1.0) is None
    manager.shutdown_all()

    assert manager._worker_thread is None
    assert manager._transports == {}


def test_manager_shuts_multiple_clients_down_concurrently(tmp_path: Path) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    clients = [_SlowShutdownClient() for _ in range(3)]
    for index, client in enumerate(clients):
        manager._transports[(LanguageId.PYTHON, tmp_path / str(index))] = client  # type: ignore[assignment]

    started_at = time.monotonic()
    completed = manager.shutdown_all(timeout=1.0)
    elapsed = time.monotonic() - started_at

    assert completed is True
    assert elapsed < 0.4
    assert all(client.closed for client in clients)
    assert len({client.deadline_at for client in clients}) == 1
    assert manager.shutdown_all(timeout=1.0) is True


def test_concurrent_shutdown_wait_respects_each_caller_deadline(
    tmp_path: Path,
) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    client = _SlowShutdownClient()
    manager._transports[(LanguageId.PYTHON, tmp_path)] = client  # type: ignore[assignment]
    first_result: list[bool] = []

    first = threading.Thread(
        target=lambda: first_result.append(manager.shutdown_all(timeout=1.0))
    )
    first.start()
    deadline = time.monotonic() + 0.5
    while client.deadline_at is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client.deadline_at is not None

    started_at = time.monotonic()
    second_result = manager.shutdown_all(timeout=0.05)
    elapsed = time.monotonic() - started_at
    first.join(timeout=1.0)

    assert second_result is False
    assert elapsed < 0.15
    assert not first.is_alive()
    assert first_result == [True]


def test_shutdown_tolerates_future_cancel_during_settlement(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    future = _CancelDuringSetFuture()
    manager._tool_queue.append(
        ToolRequest(
            file_path=source,
            language_id=LanguageId.PYTHON,
            method="test/race",
            params={},
            future=future,
            timeout_seconds=1.0,
            deadline_at=time.monotonic() + 1.0,
        )
    )

    assert manager.shutdown_all(timeout=0.5) is True
    assert future.cancelled()


def test_shutdown_does_not_interrupt_cancelled_operation_cleanup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    cleanup_started = threading.Event()
    cleanup_interrupted = threading.Event()
    cleanup_release = threading.Event()

    async def start_then_clean(
        _language: LanguageId,
        _path: Path,
        *,
        transport_key=None,
    ) -> None:
        del transport_key
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            while not cleanup_release.is_set():
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    cleanup_interrupted.set()
            raise

    manager._get_or_create_server = start_then_clean  # type: ignore[method-assign]

    with pytest.raises(LspRequestTimedOut, match="total"):
        manager.send_request_sync(source, "test/timeout", {}, timeout=0.1)
    assert cleanup_started.wait(timeout=1.0)

    shutdown_results: list[bool] = []
    shutdown = threading.Thread(
        target=lambda: shutdown_results.append(manager.shutdown_all(timeout=1.0))
    )
    shutdown.start()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        with manager._lock:
            cancelling = any(
                active.task.cancelling() for active in manager._active_work.values()
            )
        if cancelling:
            break
        time.sleep(0.005)
    else:
        cleanup_release.set()
        raise AssertionError("shutdown never cancelled active tool handler")

    cleanup_release.set()
    shutdown.join(timeout=1.0)

    assert not shutdown.is_alive()
    assert shutdown_results == [True]
    assert not cleanup_interrupted.is_set()


def test_manager_reports_incomplete_shutdown_truthfully(tmp_path: Path) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    manager._transports[(LanguageId.PYTHON, tmp_path)] = _BrokenShutdownClient()  # type: ignore[assignment]

    assert manager.shutdown_all(timeout=0.5) is False


def test_manager_aborts_unusable_live_transport(tmp_path: Path) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    client = _UnusableLiveClient()
    manager._transports[(LanguageId.PYTHON, tmp_path)] = client  # type: ignore[assignment]

    assert manager.shutdown_all(timeout=0.5) is True
    assert client.aborted is True


def test_shutdown_during_initialize_cancels_work_and_reaps_process(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "shutdown-initialize.jsonl"
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
    errors: list[BaseException] = []

    def request() -> None:
        try:
            manager.send_request_sync(source, "test/hung", {}, timeout=10.0)
        except BaseException as error:
            errors.append(error)

    request_thread = threading.Thread(target=request)
    request_thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(event["method"] == "initialize_hanging" for event in _events(log_path)):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("fake server never entered initialize hang")
    pid = int(
        next(
            event["pid"]
            for event in _events(log_path)
            if event["method"] == "server_started"
        )
    )

    started_at = time.monotonic()
    completed = manager.shutdown_all(timeout=1.0)
    elapsed = time.monotonic() - started_at
    request_thread.join(timeout=1.0)

    assert completed is True
    assert elapsed < 1.25
    assert not request_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LspClientError)
    _wait_for_pid_exit(pid)
    assert manager._worker_thread is None
    assert manager._transports == {}
