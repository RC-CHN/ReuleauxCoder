"""Observable LSP transport state and generation invariants."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import (
    MAX_TRANSPORT_STATE_HISTORY,
    MISSING_COMMAND_TTL_SECONDS,
    LspManager,
    LspTransportState,
    LspTransportStatus,
    ToolRequest,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")


def _fake_args(
    log_path: Path,
    *,
    initialize_behavior: str = "normal",
) -> list[str]:
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


def _manager(
    tmp_path: Path,
    log_path: Path,
    *,
    initialize_behavior: str = "normal",
) -> LspManager:
    return LspManager(
        LspConfig(
            server_overrides={
                "python": LspServerOverride(
                    language="python",
                    cmd=sys.executable,
                    args=_fake_args(
                        log_path,
                        initialize_behavior=initialize_behavior,
                    ),
                )
            }
        ),
        workspace_cwd=tmp_path,
    )


def _events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _server_pids(log_path: Path) -> list[int]:
    return [
        int(event["pid"])
        for event in _events(log_path)
        if event["method"] == "server_started"
    ]


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


def _states_for(manager: LspManager, path: Path) -> list[LspTransportState]:
    current = manager.transport_status_for_file(path)
    assert current is not None
    return [
        status.state
        for status in manager.transport_state_history()
        if status.language == current.language
        and status.workspace_root == current.workspace_root
    ]


def _tool_request(path: Path) -> ToolRequest:
    return ToolRequest(
        file_path=path,
        language_id=LanguageId.PYTHON,
        method="test/query",
        params={},
        future=concurrent.futures.Future(),
        timeout_seconds=2.0,
        deadline_at=time.monotonic() + 2.0,
    )


class _CancelledDiscardClient:
    is_usable = False
    is_alive = True

    async def abort(self) -> None:
        self.is_alive = False
        raise asyncio.CancelledError


def test_supported_file_is_observable_as_unstarted_generation_zero(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)

    status = manager.transport_status_for_file(path)

    assert isinstance(status, LspTransportStatus)
    assert status is not None
    assert status.language is LanguageId.PYTHON
    assert status.workspace_root == tmp_path.resolve()
    assert status.state is LspTransportState.UNSTARTED
    assert status.generation == 0
    assert status.launcher is None
    assert status.error_type is None
    assert status.error_message is None
    assert status.retry_at_monotonic is None
    assert manager.transport_statuses() == (status,)
    assert _states_for(manager, path) == [LspTransportState.UNSTARTED]


def test_state_history_is_bounded_and_scope_projection_is_secret_free(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, path)

    for _ in range(MAX_TRANSPORT_STATE_HISTORY + 10):
        generation = manager._begin_transport_attempt(
            key, "/private/credential-bin/fake-lsp"
        )
        assert manager._transition_transport(
            key,
            generation,
            LspTransportState.ERROR,
            error_type="InitializeError",
            error_message="credential=must-not-be-projected",
        )

    history = manager.transport_state_history()
    current = manager.transport_status_for_file(path)
    scopes = manager.describe_scopes()

    assert len(history) == MAX_TRANSPORT_STATE_HISTORY
    assert tuple(status.sequence for status in history) == tuple(
        sorted(status.sequence for status in history)
    )
    assert current is not None
    assert current.generation == MAX_TRANSPORT_STATE_HISTORY + 10
    assert current.error_message == "credential=must-not-be-projected"
    assert len(scopes) == 1
    assert f"g{current.generation}:error" in scopes[0]
    assert "launcher=fake-lsp" in scopes[0]
    assert "InitializeError" in scopes[0]
    assert "credential=" not in scopes[0]
    assert "/private/credential-bin" not in scopes[0]


def test_missing_launcher_negative_cache_reuses_generation_then_retries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "ttl-retry.jsonl"
    manager = _manager(tmp_path, log_path)
    now = [100.0]
    lookup = MagicMock(side_effect=[None, sys.executable])
    manager._availability_clock = lambda: now[0]
    manager._command_lookup = lookup

    async def run() -> None:
        assert await manager._spawn_async(LanguageId.PYTHON, path) is None
        missing = manager.transport_status_for_file(path)
        assert missing is not None
        assert missing.state is LspTransportState.ERROR
        assert missing.generation == 1
        assert missing.launcher == Path(sys.executable).name
        assert missing.error_type
        assert missing.error_message
        assert missing.retry_at_monotonic == pytest.approx(
            now[0] + MISSING_COMMAND_TTL_SECONDS
        )
        assert _states_for(manager, path) == [
            LspTransportState.UNSTARTED,
            LspTransportState.RESOLVING,
            LspTransportState.ERROR,
        ]

        history = manager.transport_state_history()
        now[0] += MISSING_COMMAND_TTL_SECONDS - 0.001
        assert await manager._spawn_async(LanguageId.PYTHON, path) is None
        cached = manager.transport_status_for_file(path)
        assert cached == missing
        assert manager.transport_state_history() == history
        assert lookup.call_count == 1

        now[0] += 0.001
        client = await manager._spawn_async(LanguageId.PYTHON, path)
        assert client is not None
        ready = manager.transport_status_for_file(path)
        assert ready is not None
        assert ready.state is LspTransportState.READY
        assert ready.generation == 2
        assert ready.error_type is None
        assert ready.error_message is None
        assert ready.retry_at_monotonic is None
        assert lookup.call_count == 2

        assert await manager._shutdown_clients_async(deadline_at=time.monotonic() + 2.0)

    asyncio.run(run())
    for pid in _server_pids(log_path):
        _assert_pid_exits(pid)


def test_initialize_failure_never_reports_ready(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "initialize-error.jsonl"
    manager = _manager(tmp_path, log_path, initialize_behavior="error")
    manager._command_lookup = MagicMock(return_value=sys.executable)
    assert manager.transport_status_for_file(path) is not None

    result = asyncio.run(manager._spawn_async(LanguageId.PYTHON, path))

    assert result is None
    status = manager.transport_status_for_file(path)
    assert status is not None
    assert status.state is LspTransportState.ERROR
    assert status.generation == 1
    assert status.error_type
    assert status.error_message
    states = _states_for(manager, path)
    assert states == [
        LspTransportState.UNSTARTED,
        LspTransportState.RESOLVING,
        LspTransportState.STARTING,
        LspTransportState.INITIALIZING,
        LspTransportState.ERROR,
    ]
    assert LspTransportState.READY not in states
    for pid in _server_pids(log_path):
        _assert_pid_exits(pid)


def test_old_generation_completion_and_exit_cannot_replace_current_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, path)

    generation_one = manager._begin_transport_attempt(key, "python-lsp")
    assert manager._transition_transport(
        key,
        generation_one,
        LspTransportState.INITIALIZING,
    )
    generation_two = manager._begin_transport_attempt(key, "python-lsp")
    assert manager._transition_transport(
        key,
        generation_two,
        LspTransportState.READY,
    )
    current_client = MagicMock()
    old_client = MagicMock()
    manager._transports[key] = current_client
    current = manager.transport_status_for_file(path)
    history = manager.transport_state_history()

    assert generation_one == 1
    assert generation_two == 2
    assert current is not None
    assert current.state is LspTransportState.READY
    assert current.generation == 2
    assert not manager._transition_transport(
        key,
        generation_one,
        LspTransportState.READY,
    )
    assert not manager._transition_transport(
        key,
        generation_one,
        LspTransportState.ERROR,
        error_type="process_exit",
        error_message="old reader reached EOF",
    )
    manager._on_client_exit(
        key,
        old_client,
        generation_one,
        "old process exited",
        1,
    )
    manager._on_client_exit(
        key,
        current_client,
        generation_one,
        "old generation reader exited",
        1,
    )
    assert manager.transport_status_for_file(path) == current
    assert manager.transport_state_history() == history


def test_real_initialize_and_shutdown_publish_terminal_states(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "ready-shutdown.jsonl"
    manager = _manager(tmp_path, log_path)
    manager._command_lookup = MagicMock(return_value=sys.executable)
    assert manager.transport_status_for_file(path) is not None

    async def run() -> None:
        client = await manager._spawn_async(LanguageId.PYTHON, path)
        assert client is not None
        status = manager.transport_status_for_file(path)
        assert status is not None
        assert status.state is LspTransportState.READY
        assert status.generation == 1
        assert client.is_initialized
        assert _states_for(manager, path) == [
            LspTransportState.UNSTARTED,
            LspTransportState.RESOLVING,
            LspTransportState.STARTING,
            LspTransportState.INITIALIZING,
            LspTransportState.READY,
        ]

        assert await manager._shutdown_clients_async(deadline_at=time.monotonic() + 2.0)

    asyncio.run(run())

    status = manager.transport_status_for_file(path)
    assert status is not None
    assert status.state is LspTransportState.STOPPED
    assert status.generation == 1
    assert _states_for(manager, path) == [
        LspTransportState.UNSTARTED,
        LspTransportState.RESOLVING,
        LspTransportState.STARTING,
        LspTransportState.INITIALIZING,
        LspTransportState.READY,
        LspTransportState.STOPPING,
        LspTransportState.STOPPED,
    ]
    for pid in _server_pids(log_path):
        _assert_pid_exits(pid)


def test_cancelled_discard_still_publishes_terminal_state(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, path)
    client = _CancelledDiscardClient()
    manager._transports[key] = client  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._discard_transport_async(key, client))  # type: ignore[arg-type]

    status = manager.transport_status_for_file(path)
    assert status is not None
    assert status.state is LspTransportState.STOPPED
    assert key not in manager._transports


def test_unexpected_process_exit_is_error_then_restarts(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "unexpected-exit.jsonl"
    manager = _manager(tmp_path, log_path)
    manager._command_lookup = MagicMock(return_value=sys.executable)

    async def run() -> None:
        client = await manager._spawn_async(LanguageId.PYTHON, path)
        assert client is not None
        process = client._process
        assert process is not None
        assert process.returncode is None

        process.terminate()
        watcher = client._process_wait_task
        assert watcher is not None
        await asyncio.wait_for(watcher, timeout=1.0)
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            status = manager.transport_status_for_file(path)
            if status is not None and status.state is LspTransportState.ERROR:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("unexpected process exit never reached error state")

        status = manager.transport_status_for_file(path)
        assert status is not None
        assert status.state is LspTransportState.ERROR
        assert status.generation == 1
        assert status.error_type in {"ProcessExited", "TransportClosed"}
        assert status.error_message
        assert not client.is_alive

        restarted = await manager._get_or_create_server(LanguageId.PYTHON, path)
        assert restarted is not None
        assert restarted is not client
        status = manager.transport_status_for_file(path)
        assert status is not None
        assert status.state is LspTransportState.READY
        assert status.generation == 2
        assert await manager._shutdown_clients_async(deadline_at=time.monotonic() + 2.0)

    asyncio.run(run())
    pids = _server_pids(log_path)
    assert len(pids) == 2
    for pid in pids:
        _assert_pid_exits(pid)


def test_new_generation_forgets_sync_and_sends_did_open_again(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    log_path = tmp_path / "generation-sync.jsonl"
    manager = _manager(tmp_path, log_path)
    manager._command_lookup = MagicMock(return_value=sys.executable)

    async def run() -> None:
        first = await manager._spawn_async(LanguageId.PYTHON, path)
        assert first is not None
        assert await manager._execute_tool_request(_tool_request(path)) is None
        assert first.document_version(path) == 1

        await first.abort()
        second = await manager._get_or_create_server(LanguageId.PYTHON, path)
        assert second is not None
        assert second is not first
        assert await manager._execute_tool_request(_tool_request(path)) is None
        assert second.document_version(path) == 1

        status = manager.transport_status_for_file(path)
        assert status is not None
        assert status.state is LspTransportState.READY
        assert status.generation == 2
        assert await manager._shutdown_clients_async(deadline_at=time.monotonic() + 2.0)

    asyncio.run(run())

    received = [
        event["method"]
        for event in _events(log_path)
        if event["direction"] == "recv"
        and event["method"] in {"textDocument/didOpen", "textDocument/didChange"}
    ]
    assert received == ["textDocument/didOpen", "textDocument/didOpen"]
    assert len(_server_pids(log_path)) == 2
    for pid in _server_pids(log_path):
        _assert_pid_exits(pid)
