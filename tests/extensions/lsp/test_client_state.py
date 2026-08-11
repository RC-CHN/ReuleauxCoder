from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from reuleauxcoder.extensions.lsp.client import LspClient, LspProtocolMessageError
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _client(tmp_path: Path) -> LspClient:
    return LspClient(LanguageId.PYTHON, tmp_path)


class _GatedStdin:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.frames: list[bytes] = []
        self.active_drains = 0
        self.max_active_drains = 0
        self.closed = False

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        self.active_drains += 1
        self.max_active_drains = max(self.max_active_drains, self.active_drains)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.active_drains -= 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _decode_frame(frame: bytes) -> dict:
    return json.loads(frame.split(b"\r\n\r\n", 1)[1])


def test_document_versions_increase_monotonically(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._send_notification = AsyncMock()
    path = tmp_path / "main.py"

    async def run() -> None:
        await client.did_open(path, "x = 1")
        await client.did_change(path, "x = 2")
        await client.did_change(path, "x = 3")

    asyncio.run(run())

    calls = client._send_notification.await_args_list
    assert calls[0].args[1]["textDocument"]["version"] == 1
    assert calls[1].args[1]["textDocument"]["version"] == 2
    assert calls[2].args[1]["textDocument"]["version"] == 3


def test_publish_diagnostics_replaces_and_empty_clears(tmp_path: Path) -> None:
    client = _client(tmp_path)
    uri = (tmp_path / "main.py").resolve().as_uri()
    first = {
        "uri": uri,
        "diagnostics": [
            {
                "range": {
                    "start": {"line": 0, "character": 1},
                    "end": {"line": 0, "character": 2},
                },
                "message": "old",
                "severity": 1,
            }
        ],
    }

    client._handle_publish_diagnostics(first)
    assert [item.message for item in client._diagnostics_buffer[uri]] == ["old"]

    client._handle_publish_diagnostics({"uri": uri, "diagnostics": []})
    assert uri in client._diagnostics_buffer
    assert client._diagnostics_buffer[uri] == []
    assert client.diagnostics_generation(tmp_path / "main.py") == 2


def test_publish_diagnostics_rejects_older_document_version(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    uri = path.resolve().as_uri()
    client._document_versions[uri] = 3

    client._handle_publish_diagnostics({"uri": uri, "version": 2, "diagnostics": []})

    assert client.diagnostics_generation(path) == 0
    assert uri not in client._diagnostics_buffer

    client._handle_publish_diagnostics({"uri": uri, "version": 3, "diagnostics": []})

    assert client.diagnostics_generation(path) == 1
    assert client.diagnostic_document_version(path) == 3


def test_wait_for_diagnostics_rejects_preexisting_stale_batch(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    uri = path.resolve().as_uri()
    client._handle_publish_diagnostics(
        {
            "uri": uri,
            "diagnostics": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                    "message": "stale",
                }
            ],
        }
    )
    baseline = client.diagnostics_generation(path)

    async def run():
        async def publish_new() -> None:
            await asyncio.sleep(0.02)
            client._handle_publish_diagnostics(
                {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 1, "character": 0},
                                "end": {"line": 1, "character": 1},
                            },
                            "message": "fresh",
                        }
                    ],
                }
            )

        publisher = asyncio.create_task(publish_new())
        diagnostics = await client.wait_for_diagnostics(
            path, timeout=0.5, after_generation=baseline
        )
        await publisher
        return diagnostics

    diagnostics = asyncio.run(run())

    assert [item.message for item in diagnostics] == ["fresh"]


def test_wait_timeout_does_not_return_preexisting_batch(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    client._handle_publish_diagnostics(
        {"uri": path.resolve().as_uri(), "diagnostics": []}
    )
    baseline = client.diagnostics_generation(path)

    diagnostics = asyncio.run(
        client.wait_for_diagnostics(path, timeout=0.01, after_generation=baseline)
    )

    assert diagnostics == []


def test_wait_without_baseline_consumes_already_published_batch(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    client._handle_publish_diagnostics(
        {
            "uri": path.resolve().as_uri(),
            "diagnostics": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                    "message": "ready",
                }
            ],
        }
    )

    diagnostics = asyncio.run(client.wait_for_diagnostics(path, timeout=0.01))

    assert [item.message for item in diagnostics] == ["ready"]


def test_pull_diagnostics_full_and_unchanged_track_fresh_versions(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    client._supports_pull_diagnostics = True
    client._send_notification = AsyncMock()
    client._send_request = AsyncMock(
        side_effect=[
            {
                "kind": "full",
                "resultId": "result-1",
                "items": [
                    {
                        "range": {
                            "start": {"line": 1, "character": 2},
                            "end": {"line": 1, "character": 3},
                        },
                        "message": "broken",
                        "severity": 1,
                    }
                ],
            },
            {"kind": "unchanged", "resultId": "result-1"},
        ]
    )

    async def run() -> tuple[list, list]:
        await client.did_open(path, "broken")
        await client.refresh_diagnostics(path)
        first = await client.wait_for_diagnostics(path, timeout=0.01)
        baseline = client.diagnostics_generation(path)
        await client.did_change(path, "still broken")
        await client.refresh_diagnostics(path)
        second = await client.wait_for_diagnostics(
            path, timeout=0.01, after_generation=baseline
        )
        return first, second

    first, second = asyncio.run(run())

    assert [item.message for item in first] == ["broken"]
    assert second == first
    assert client.diagnostics_generation(path) == 2
    assert client.diagnostic_document_version(path) == 2
    second_params = client._send_request.await_args_list[1].args[1]
    assert second_params["previousResultId"] == "result-1"


def test_document_sync_does_not_inline_pull_diagnostics(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    client._supports_pull_diagnostics = True
    client._send_notification = AsyncMock()
    client._send_request = AsyncMock(side_effect=RuntimeError("pull failed"))

    asyncio.run(client.did_open(path, "value = 1"))

    assert client.document_version(path) == 1
    client._send_notification.assert_awaited_once()
    client._send_request.assert_not_awaited()


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"kind": "unknown"},
        {"kind": "full", "items": {}},
        {"kind": "full", "items": [{}]},
        {"kind": "unchanged", "resultId": 7},
    ],
)
def test_malformed_pull_diagnostics_fails_transport(
    tmp_path: Path,
    result: object,
) -> None:
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )
    client._supports_pull_diagnostics = True
    client._send_request = AsyncMock(return_value=result)
    process = MagicMock(returncode=None)
    client._process = process

    with pytest.raises(LspProtocolMessageError):
        asyncio.run(client._pull_document_diagnostics(tmp_path / "main.py"))

    assert not client.is_usable
    process.kill.assert_called_once_with()
    assert [(reason, returncode) for _, reason, returncode in callbacks] == [
        ("protocol message error: LspProtocolMessageError", None)
    ]


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("textDocument/definition", {}),
        ("textDocument/definition", [None]),
        ("textDocument/references", {}),
        ("textDocument/references", [{"uri": "file:///tmp/main.py"}]),
        ("textDocument/documentSymbol", {}),
        ("textDocument/documentSymbol", [{}]),
    ],
)
def test_malformed_active_result_fails_transport(
    tmp_path: Path,
    method: str,
    result: object,
) -> None:
    callbacks: list[tuple[LspClient, str, int | None]] = []
    client = LspClient(
        LanguageId.PYTHON,
        tmp_path,
        on_unexpected_exit=lambda *event: callbacks.append(event),
    )
    client._send_request = AsyncMock(return_value=result)
    process = MagicMock(returncode=None)
    client._process = process

    with pytest.raises(LspProtocolMessageError):
        asyncio.run(client.send_request(method, {}))

    assert not client.is_usable
    process.kill.assert_called_once_with()
    assert callbacks[0][1] == "protocol message error: LspProtocolMessageError"


def test_initialize_detects_pull_diagnostic_capability(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client._process = object()  # type: ignore[assignment]
    client._send_request = AsyncMock(
        return_value={
            "capabilities": {"diagnosticProvider": {"interFileDependencies": True}},
            "serverInfo": {"name": "native-test"},
        }
    )
    client._send_notification = AsyncMock()

    asyncio.run(client.initialize())

    assert client._supports_pull_diagnostics is True
    params = client._send_request.await_args.args[1]
    assert params["capabilities"]["textDocument"]["diagnostic"] == {
        "dynamicRegistration": False,
        "relatedDocumentSupport": False,
    }


def test_server_configuration_request_receives_json_rpc_response(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    client._write_message = AsyncMock()

    async def run() -> None:
        client._dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "workspace/configuration",
                "params": {"items": [{"section": "formatting"}]},
            }
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    client._write_message.assert_awaited_once_with(
        {"jsonrpc": "2.0", "id": 7, "result": [{}]}
    )


def test_server_response_waits_for_active_stdin_write(tmp_path: Path) -> None:
    client = _client(tmp_path)

    async def run() -> tuple[list[bytes], int]:
        stdin = _GatedStdin()
        client._process = SimpleNamespace(stdin=stdin)  # type: ignore[assignment]
        notification = asyncio.create_task(client.send_notification("test/first", {}))
        await asyncio.wait_for(stdin.entered.wait(), timeout=0.5)
        client._dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "workspace/configuration",
                "params": {"items": []},
            }
        )
        response = next(iter(client._server_response_tasks))
        await asyncio.sleep(0)

        assert len(stdin.frames) == 1
        assert stdin.max_active_drains == 1
        stdin.release.set()
        await notification
        await response
        return stdin.frames, stdin.max_active_drains

    frames, max_active_drains = asyncio.run(run())

    assert [_decode_frame(frame) for frame in frames] == [
        {"jsonrpc": "2.0", "method": "test/first", "params": {}},
        {"jsonrpc": "2.0", "id": 9, "result": []},
    ]
    assert max_active_drains == 1


def test_abort_collects_tracked_server_response_write(tmp_path: Path) -> None:
    client = _client(tmp_path)

    async def run() -> tuple[asyncio.Task[None], _GatedStdin]:
        stdin = _GatedStdin()
        client._process = SimpleNamespace(  # type: ignore[assignment]
            stdin=stdin,
            returncode=0,
        )
        client._dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "workspace/configuration",
                "params": {"items": []},
            }
        )
        response = next(iter(client._server_response_tasks))
        await asyncio.wait_for(stdin.entered.wait(), timeout=0.5)

        await asyncio.wait_for(client.abort(), timeout=0.5)
        return response, stdin

    response, stdin = asyncio.run(run())

    assert response.done()
    assert response.cancelled()
    assert stdin.closed
    assert not client._stdin_write_lock.locked()
    assert client._reader_task is None
    assert client._process is None


def test_shutdown_deadline_includes_waiting_for_stdin_lock(tmp_path: Path) -> None:
    client = _client(tmp_path)

    async def run() -> tuple[float, asyncio.Task[None], _GatedStdin]:
        stdin = _GatedStdin()
        client._process = SimpleNamespace(  # type: ignore[assignment]
            stdin=stdin,
            returncode=0,
        )
        reader = asyncio.create_task(client.send_notification("test/blocked", {}))
        client._reader_task = reader
        await asyncio.wait_for(stdin.entered.wait(), timeout=0.5)

        started_at = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            client.shutdown(deadline_at=started_at + 0.4),
            timeout=0.7,
        )
        return asyncio.get_running_loop().time() - started_at, reader, stdin

    elapsed, reader, stdin = asyncio.run(run())

    assert elapsed < 0.7
    assert reader.done()
    assert reader.cancelled()
    assert stdin.closed
    assert not client._stdin_write_lock.locked()
    assert client._pending == {}
    assert client._process is None
