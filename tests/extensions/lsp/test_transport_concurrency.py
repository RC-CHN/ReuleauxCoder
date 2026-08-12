"""Deterministic concurrency invariants for workspace-scoped LSP transports."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from reuleauxcoder.extensions.lsp.client import (
    LspClientError,
    LspRequestTimedOut,
)
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId
from tests.process_helpers import process_is_alive as _pid_alive

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")
TRACE_NAME = "trace.jsonl"


def _server_args(
    mode: str,
    *,
    block_method: str | None = None,
    block_until: str | None = None,
    first_save_gate: str | None = None,
) -> list[str]:
    args = [
        "-u",
        str(FAKE_SERVER),
        "--mode",
        mode,
        "--log",
        TRACE_NAME,
    ]
    if block_method is not None and block_until is not None:
        args.extend(("--block-method", block_method, "--block-until", block_until))
    if first_save_gate is not None:
        args.extend(("--block-first-save-until", first_save_gate))
    return args


def _manager(
    tmp_path: Path,
    *,
    mode: str,
    block_method: str | None = None,
    block_until: str | None = None,
    first_save_gate: str | None = None,
) -> LspManager:
    manager = LspManager(
        LspConfig(
            poll_timeout_ms=3_000,
            server_overrides={
                "python": LspServerOverride(
                    language="python",
                    cmd=sys.executable,
                    args=_server_args(
                        mode,
                        block_method=block_method,
                        block_until=block_until,
                        first_save_gate=first_save_gate,
                    ),
                )
            },
        ),
        workspace_cwd=tmp_path,
    )
    manager._availability[LanguageId.PYTHON] = True
    return manager


def _workspace_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    roots = (tmp_path / "workspace-a", tmp_path / "workspace-b")
    paths: list[Path] = []
    for index, root in enumerate(roots):
        root.mkdir()
        (root / "pyproject.toml").write_text(
            f"[project]\nname = 'transport-{index}'\n",
            encoding="utf-8",
        )
        path = root / "main.py"
        path.write_text(f"value = {index}\n", encoding="utf-8")
        paths.append(path)
    return roots[0], roots[1], paths[0], paths[1]


def _events(root: Path) -> list[dict[str, Any]]:
    log_path = root / TRACE_NAME
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for deterministic LSP event")


def _server_pids(*roots: Path) -> list[int]:
    return [
        int(event["pid"])
        for root in roots
        for event in _events(root)
        if event.get("method") == "server_started"
    ]


def _assert_pid_exits(pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.01)
    assert not _pid_alive(pid)


class _ControlledClient:
    """Async client double exposing actual overlap and cancellation."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    @property
    def is_alive(self) -> bool:
        return True

    @property
    def is_usable(self) -> bool:
        return True

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
        del timeout
        self.calls.append(method)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if method.startswith("slow"):
                self.started.set()
                while not self.release.is_set():
                    await asyncio.sleep(0.01)
            return method
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1


def test_slow_query_in_one_workspace_does_not_block_other_transport(
    tmp_path: Path,
) -> None:
    root_a, root_b, path_a, path_b = _workspace_roots(tmp_path)
    gate = root_a / "release-slow"
    manager = _manager(
        tmp_path,
        mode="push",
        block_method="test/slow",
        block_until=gate.name,
    )
    slow_results: list[Any] = []
    slow_errors: list[BaseException] = []

    def run_slow() -> None:
        try:
            slow_results.append(
                manager.send_request_sync(path_a, "test/slow", {}, timeout=5.0)
            )
        except BaseException as error:
            slow_errors.append(error)

    thread = threading.Thread(target=run_slow)
    thread.start()
    try:
        _wait_until(
            lambda: any(
                event.get("method") == "request_blocked"
                and event.get("blocked_method") == "test/slow"
                for event in _events(root_a)
            )
        )

        assert manager.send_request_sync(path_b, "test/fast", {}, timeout=1.5) is None
        assert thread.is_alive()
        assert not gate.exists()
        assert any(
            event.get("method") == "response:test/fast" for event in _events(root_b)
        )
        assert not any(
            event.get("method") == "request_released" for event in _events(root_a)
        )
    finally:
        gate.touch(exist_ok=True)
        thread.join(timeout=3.0)
        manager.shutdown_all(timeout=2.0)

    assert not thread.is_alive()
    assert slow_errors == []
    assert slow_results == [None]
    pids = _server_pids(root_a, root_b)
    assert len(pids) == 2
    for pid in pids:
        _assert_pid_exits(pid)


def test_same_transport_concurrent_cold_start_spawns_one_process(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    gate = tmp_path / "release-initialize"
    manager = _manager(
        tmp_path,
        mode="push",
        block_method="initialize",
        block_until=gate.name,
    )
    results: list[Any] = []
    errors: list[BaseException] = []

    def request(method: str) -> None:
        try:
            results.append(manager.send_request_sync(path, method, {}, timeout=4.0))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=request, args=("test/first",)),
        threading.Thread(target=request, args=("test/second",)),
    ]
    threads[0].start()
    try:
        _wait_until(
            lambda: any(
                event.get("method") == "request_blocked"
                and event.get("blocked_method") == "initialize"
                for event in _events(tmp_path)
            )
        )
        threads[1].start()
        _wait_until(lambda: len(manager._tool_queue) == 1)

        assert len(_server_pids(tmp_path)) == 1
        gate.touch()
        for thread in threads:
            thread.join(timeout=3.0)
    finally:
        gate.touch(exist_ok=True)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3.0)
        manager.shutdown_all(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert results == [None, None]
    pids = _server_pids(tmp_path)
    assert len(pids) == 1
    _assert_pid_exits(pids[0])


def test_same_transport_is_serial_and_timed_out_waiter_never_enters_client(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    client = _ControlledClient()
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def get_client(
        _language: LanguageId,
        _path: Path,
        *,
        transport_key: object | None = None,
    ) -> _ControlledClient:
        del transport_key
        return client

    manager._get_or_create_server = get_client  # type: ignore[method-assign]
    first_results: list[str] = []
    first_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            first_results.append(
                manager.send_request_sync(path, "slow", {}, timeout=3.0)
            )
        except BaseException as error:
            first_errors.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert client.started.wait(timeout=1.0)

        with pytest.raises(LspRequestTimedOut, match="total"):
            manager.send_request_sync(path, "second", {}, timeout=0.2)

        assert client.calls == ["slow"]
        assert client.max_active == 1
        client.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert manager.send_request_sync(path, "after", {}, timeout=1.0) == "after"
    finally:
        client.release.set()
        thread.join(timeout=2.0)
        manager.shutdown_all(timeout=1.0)

    assert first_errors == []
    assert first_results == ["slow"]
    assert client.calls == ["slow", "after"]
    assert client.max_active == 1


def test_diagnostics_wait_does_not_block_query_on_other_transport(
    tmp_path: Path,
) -> None:
    root_a, root_b, path_a, path_b = _workspace_roots(tmp_path)
    gate = root_a / "release-save"
    manager = _manager(
        tmp_path,
        mode="save-only",
        first_save_gate=gate.name,
    )
    batch_id: str | None = None
    try:
        assert manager.send_request_sync(path_a, "test/warm", {}, timeout=2.0) is None
        assert manager.send_request_sync(path_b, "test/warm", {}, timeout=2.0) is None

        path_a.write_text(
            "# FAKE_LSP_ERROR: delayed diagnostics\n",
            encoding="utf-8",
        )
        batch_id = manager.enqueue_diagnostics(
            path_a,
            route=DiagnosticRoute(
                file_path=path_a,
                agent_id="parent",
                session_generation=1,
                session_id="session",
                turn_id="turn",
                tool_call_id="edit",
            ),
            document_committed=True,
        )
        assert batch_id is not None
        _wait_until(
            lambda: any(
                event.get("method") == "first_save_blocked" for event in _events(root_a)
            )
        )
        assert manager.diagnostic_request_result(batch_id) is None

        assert manager.send_request_sync(path_b, "test/fast", {}, timeout=1.0) is None
        assert manager.diagnostic_request_result(batch_id) is None
        assert not gate.exists()

        gate.touch()
        _wait_until(lambda: manager.diagnostic_request_result(batch_id) is not None)
        batches = manager.diagnostic_request_result(batch_id)
        assert batches is not None
        assert len(batches) == 1
        assert [item.message for item in batches[0].block.items] == [
            "delayed diagnostics"
        ]
    finally:
        gate.touch(exist_ok=True)
        manager.shutdown_all(timeout=2.0)

    pids = _server_pids(root_a, root_b)
    assert len(pids) == 2
    for pid in pids:
        _assert_pid_exits(pid)


def test_shutdown_collects_all_inflight_transport_tasks(tmp_path: Path) -> None:
    root_a, root_b, path_a, path_b = _workspace_roots(tmp_path)
    clients = {
        root_a.resolve(): _ControlledClient(),
        root_b.resolve(): _ControlledClient(),
    }
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def get_client(
        _language: LanguageId,
        path: Path,
        *,
        transport_key: object | None = None,
    ) -> _ControlledClient:
        del transport_key
        return clients[path.resolve().parent]

    manager._get_or_create_server = get_client  # type: ignore[method-assign]
    results: list[str] = []
    errors: list[BaseException] = []

    def request(path: Path, method: str) -> None:
        try:
            results.append(manager.send_request_sync(path, method, {}, timeout=10.0))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=request, args=(path_a, "slow-a")),
        threading.Thread(target=request, args=(path_b, "slow-b")),
    ]
    for thread in threads:
        thread.start()

    completed = False
    try:
        _wait_until(lambda: all(client.started.is_set() for client in clients.values()))
        started_at = time.monotonic()
        completed = manager.shutdown_all(timeout=1.0)
        elapsed = time.monotonic() - started_at
    finally:
        for client in clients.values():
            client.release.set()
        for thread in threads:
            thread.join(timeout=2.0)
        if manager._worker_thread is not None:
            manager.shutdown_all(timeout=1.0)

    assert completed is True
    assert elapsed < 1.25
    assert all(not thread.is_alive() for thread in threads)
    assert results == []
    assert len(errors) == 2
    assert all(isinstance(error, LspClientError) for error in errors)
    assert all(client.cancelled.is_set() for client in clients.values())
    assert manager._worker_thread is None
    assert manager._worker_loop is None
    assert manager._transports == {}
    assert manager._active_work == {}


def test_shutdown_settles_active_and_queued_work_for_same_transport(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    client = _ControlledClient()
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def get_client(
        _language: LanguageId,
        _path: Path,
        *,
        transport_key: object | None = None,
    ) -> _ControlledClient:
        del transport_key
        return client

    manager._get_or_create_server = get_client  # type: ignore[method-assign]
    results: list[str] = []
    errors: list[BaseException] = []

    def request(method: str) -> None:
        try:
            results.append(manager.send_request_sync(path, method, {}, timeout=10.0))
        except BaseException as error:
            errors.append(error)

    active = threading.Thread(target=request, args=("slow-active",))
    queued = threading.Thread(target=request, args=("queued",))
    active.start()
    assert client.started.wait(timeout=1.0)
    queued.start()
    _wait_until(lambda: len(manager._tool_queue) == 1)

    completed = manager.shutdown_all(timeout=1.0)
    active.join(timeout=1.0)
    queued.join(timeout=1.0)

    assert completed is True
    assert not active.is_alive()
    assert not queued.is_alive()
    assert results == []
    assert len(errors) == 2
    assert all(isinstance(error, LspClientError) for error in errors)
    assert client.calls == ["slow-active"]
    assert client.cancelled.is_set()
    assert manager._tool_queue == []
    assert manager._active_work == {}
