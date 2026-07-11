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
    assert client.diagnostics_generation(tmp_path / "main.py") == 2


def test_publish_diagnostics_rejects_older_document_version(tmp_path: Path) -> None:
    client = _client(tmp_path)
    path = tmp_path / "main.py"
    uri = path.resolve().as_uri()
    client._document_versions[uri] = 3

    client._handle_publish_diagnostics(
        {"uri": uri, "version": 2, "diagnostics": []}
    )

    assert client.diagnostics_generation(path) == 0
    assert uri not in client._diagnostics_buffer

    client._handle_publish_diagnostics(
        {"uri": uri, "version": 3, "diagnostics": []}
    )

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
                    "range": {"start": {"line": 0, "character": 0}},
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
                            "range": {"start": {"line": 1, "character": 0}},
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
        client.wait_for_diagnostics(
            path, timeout=0.01, after_generation=baseline
        )
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
                    "range": {"start": {"line": 0, "character": 0}},
                    "message": "ready",
                }
            ],
        }
    )

    diagnostics = asyncio.run(client.wait_for_diagnostics(path, timeout=0.01))

    assert [item.message for item in diagnostics] == ["ready"]
