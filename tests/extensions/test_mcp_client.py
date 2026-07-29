import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcomeStatus
from reuleauxcoder.extensions.mcp.adapter import MCPTool
from reuleauxcoder.extensions.mcp.client import (
    MCPClient,
    MCPRequestTransportLost,
    MCPToolRequestCancelled,
)
from reuleauxcoder.extensions.mcp.models import MCPToolInfo


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


def _client() -> tuple[MCPClient, _Writer]:
    config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
    client = MCPClient(config)
    writer = _Writer()
    client._writer = writer
    client._reader = object()
    return client, writer


def test_mcp_cancel_removes_pending_then_notifies_exact_request_id() -> None:
    async def scenario() -> None:
        client, writer = _client()
        cancellation = threading.Event()
        handle = await client._request("tools/call", {"name": "mutate"})
        cancellation.set()

        with pytest.raises(MCPToolRequestCancelled):
            await client._await_request(
                handle,
                cancellation_signal=cancellation,
            )

        assert handle.request_id not in client._pending_requests
        assert handle.cancellation_sent is True
        assert len(writer.writes) == 2
        notification = json.loads(writer.writes[-1])
        assert notification == {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": handle.request_id,
                "reason": "User interrupted the active tool call",
            },
        }

    asyncio.run(scenario())


def test_mcp_cancel_does_not_wait_for_notification_backpressure() -> None:
    class _BackpressuredWriter(_Writer):
        def __init__(self) -> None:
            super().__init__()
            self.drain_calls = 0
            self.block = asyncio.Event()

        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls > 1:
                await self.block.wait()

    async def scenario() -> None:
        config = SimpleNamespace(
            name="test", command="test", args=[], env={}, cwd=None
        )
        client = MCPClient(config)
        writer = _BackpressuredWriter()
        client._writer = writer
        client._reader = object()
        handle = await client._request("tools/call", {"name": "mutate"})
        cancellation = threading.Event()
        cancellation.set()

        started = time.monotonic()
        with pytest.raises(MCPToolRequestCancelled):
            await client._await_request(
                handle,
                cancellation_signal=cancellation,
            )

        assert time.monotonic() - started < 0.1
        assert len(writer.writes) == 2

    asyncio.run(scenario())


def test_mcp_response_already_settled_wins_over_cancel_signal() -> None:
    async def scenario() -> None:
        client, writer = _client()
        cancellation = threading.Event()
        handle = await client._request("tools/call", {"name": "read"})
        client._pending_requests.pop(handle.request_id)
        handle.future.set_result({"content": []})
        cancellation.set()

        assert (
            await client._await_request(
                handle,
                cancellation_signal=cancellation,
            )
            == {"content": []}
        )
        assert len(writer.writes) == 1

    asyncio.run(scenario())


def test_in_flight_tool_transport_loss_is_not_retried() -> None:
    class _Client(MCPClient):
        reconnect_calls = 0

        def is_connected(self) -> bool:
            return True

        async def reconnect(self) -> bool:
            self.reconnect_calls += 1
            return True

    async def scenario() -> None:
        config = SimpleNamespace(
            name="test", command="test", args=[], env={}, cwd=None
        )
        client = _Client(config)
        client._initialized = True
        client._writer = _Writer()
        client._reader = object()

        task = asyncio.create_task(client.call_tool("mutate", {}))
        while not client._pending_requests:
            await asyncio.sleep(0)
        client._fail_pending(MCPRequestTransportLost("connection lost"))
        with pytest.raises(MCPRequestTransportLost) as raised:
            await task

        assert raised.value.request_id is not None
        assert client.reconnect_calls == 0

    asyncio.run(scenario())


def test_mcp_adapter_reports_cancelled_with_unknown_effect_state() -> None:
    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            raise MCPToolRequestCancelled(42)

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert ready.wait(timeout=2)
    try:
        tool = MCPTool(
            _Client(),
            MCPToolInfo(
                name="mutate",
                description="Mutate",
                input_schema={"type": "object"},
                server_name="test",
            ),
            loop,
        )
        outcome = tool.execute()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert outcome.status is ToolOutcomeStatus.CANCELLED
    assert outcome.metadata["effect_state"] == "unknown"
    assert "do not blindly repeat" in outcome.model_text


def test_mcp_adapter_reports_lost_in_flight_result_as_unknown() -> None:
    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            raise MCPRequestTransportLost("connection lost", request_id=17)

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert ready.wait(timeout=2)
    try:
        tool = MCPTool(
            _Client(),
            MCPToolInfo(
                name="mutate",
                description="Mutate",
                input_schema={"type": "object"},
                server_name="test",
            ),
            loop,
        )
        outcome = tool.execute()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.metadata == {
        "effect_state": "unknown",
        "mcp_server": "test",
        "mcp_request_id": 17,
    }
    assert "operation may have completed" in outcome.model_text
