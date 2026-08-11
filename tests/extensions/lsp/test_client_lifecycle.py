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

from reuleauxcoder.extensions.lsp.client import LspClient, LspClientError
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")


def _requestable_client(tmp_path: Path) -> LspClient:
    client = LspClient(LanguageId.PYTHON, tmp_path)
    client._process = MagicMock(stdin=object())
    return client


def _fake_args(log_path: Path, *, initialize_behavior: str = "normal") -> list[str]:
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

    with pytest.raises(LspClientError, match="timed out"):
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
        client._dispatch_message(
            {"jsonrpc": "2.0", "id": request_id, "result": "late"}
        )
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


def test_abort_force_closes_real_stdio_process_idempotently(tmp_path: Path) -> None:
    log_path = tmp_path / "abort.jsonl"
    client = LspClient(LanguageId.PYTHON, tmp_path)

    async def run() -> int:
        await client.spawn(sys.executable, _fake_args(log_path))
        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            not any(
                event["method"] == "server_started"
                for event in _events(log_path)
            )
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        pid = _server_pid(log_path)
        assert _pid_alive(pid)

        await client.abort()
        await client.abort()

        assert client._process is None
        assert client._reader_task is None
        assert client._pending == {}
        return pid

    pid = asyncio.run(run())
    _assert_pid_exits(pid)


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
                event["method"] == "initialize_hanging"
                for event in _events(log_path)
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
