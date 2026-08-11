"""Cancellation and cleanup invariants for the stdio LSP client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from reuleauxcoder.extensions.lsp.client import LspClient, LspClientError
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _requestable_client(tmp_path: Path) -> LspClient:
    client = LspClient(LanguageId.PYTHON, tmp_path)
    client._process = MagicMock(stdin=object())
    return client


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
