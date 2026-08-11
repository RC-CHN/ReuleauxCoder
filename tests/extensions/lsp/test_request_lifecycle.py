"""End-to-end deadlines for the synchronous LSP manager bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reuleauxcoder.extensions.lsp.client import LspRequestTimedOut
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import LspManager
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


def _manager_with_server(tmp_path: Path, server: _ControlledServer) -> LspManager:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def get_server(_language: LanguageId, _path: Path):
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
        first_result.append(
            manager.send_request_sync(source, "slow", {}, timeout=2.0)
        )

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
        manager.send_request_sync(source, "slow", {}, timeout=0.08)

    assert server.cancelled.wait(timeout=1.0)
    assert manager.send_request_sync(source, "fast", {}, timeout=1.0) == "fast"
    assert server.calls == ["slow", "fast"]
    manager.shutdown_all()


def test_query_receives_only_remaining_end_to_end_budget(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    server = _ControlledServer()
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    async def delayed_start(_language: LanguageId, _path: Path):
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
