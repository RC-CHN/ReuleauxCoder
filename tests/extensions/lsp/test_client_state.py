from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from reuleauxcoder.extensions.lsp.client import LspClient
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _client(tmp_path: Path) -> LspClient:
    return LspClient(LanguageId.PYTHON, tmp_path)


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
                "range": {"start": {"line": 0, "character": 1}},
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
