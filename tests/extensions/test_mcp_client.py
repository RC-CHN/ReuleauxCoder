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
    MCPToolResultProtocolError,
    MCPToolRequestCancelled,
)
from reuleauxcoder.extensions.mcp.models import (
    MCPRequestHandle,
    MCPToolCallResult,
    MCPToolInfo,
)


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
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
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

        assert await client._await_request(
            handle,
            cancellation_signal=cancellation,
        ) == {"content": []}
        assert len(writer.writes) == 1

    asyncio.run(scenario())


def test_mcp_client_preserves_server_reported_error_identity() -> None:
    class _Client(MCPClient):
        def is_connected(self) -> bool:
            return True

        async def _request(self, method, params):  # noqa: ARG002
            return MCPRequestHandle(
                request_id=37,
                method="tools/call",
                future=asyncio.get_running_loop().create_future(),
            )

        async def _await_request(self, handle, **kwargs):  # noqa: ARG002
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Invalid argument: path is required",
                    }
                ],
                "isError": True,
            }

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        client = _Client(config)
        client._initialized = True

        result = await client.call_tool("mutate", {})

        assert result == MCPToolCallResult(
            content="Invalid argument: path is required",
            is_error=True,
            request_id=37,
            error_content_items=1,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (None, "result_not_object"),
        ({}, "content_not_array"),
        ({"content": [], "isError": "true"}, "is_error_not_boolean"),
        ({"content": "not-an-array"}, "content_not_array"),
        ({"content": [42]}, "content_item_not_object"),
        (
            {"content": [{"type": "text", "text": 42}]},
            "text_content_invalid",
        ),
        (
            {"content": [{"type": "future-content"}]},
            "unsupported_content_type",
        ),
    ],
)
def test_mcp_client_rejects_malformed_tool_result(payload, code) -> None:
    class _Client(MCPClient):
        def is_connected(self) -> bool:
            return True

        async def _request(self, method, params):  # noqa: ARG002
            return MCPRequestHandle(
                request_id=38,
                method="tools/call",
                future=asyncio.get_running_loop().create_future(),
            )

        async def _await_request(self, handle, **kwargs):  # noqa: ARG002
            return payload

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        client = _Client(config)
        client._initialized = True

        with pytest.raises(MCPToolResultProtocolError) as raised:
            await client.call_tool("mutate", {})

        assert raised.value.code == code
        assert raised.value.request_id == 38

    asyncio.run(scenario())


def test_mcp_adapter_projects_reported_error_as_safe_failure() -> None:
    business_error = "Invalid argument: path is required"

    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            return MCPToolCallResult(
                content=business_error,
                is_error=True,
                request_id=41,
                error_content_items=1,
            )

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
        "failure_phase": "tool_result",
        "error_type": "MCPToolReportedError",
        "mcp_request_id": 41,
        "effect_state": "server_reported_failure",
        "error_detail_state": "server_error_content",
        "error_content_items": 1,
    }
    assert "error_type=MCPToolReportedError" in outcome.model_text
    assert "request_id=41" in outcome.model_text
    assert "details=server_error_content" in outcome.model_text
    assert business_error in outcome.model_text
    assert business_error in outcome.display_text


def test_mcp_adapter_projects_protocol_error_as_safe_failure() -> None:
    secret = "SENTINEL_PROTOCOL_SECRET"

    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            raise MCPToolResultProtocolError(secret, request_id=43)

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
        "failure_phase": "tool_result_protocol",
        "error_type": "MCPToolResultProtocolError",
        "protocol_error_code": "invalid_tool_result",
        "mcp_request_id": 43,
        "effect_state": "unknown",
    }
    assert "error_type=MCPToolResultProtocolError" in outcome.model_text
    assert "protocol_error_code=invalid_tool_result" in outcome.model_text
    assert secret not in outcome.model_text
    assert secret not in outcome.display_text
    assert secret not in repr(outcome.metadata)


def test_mcp_adapter_rejects_unknown_result_type_as_safe_failure() -> None:
    secret = "SENTINEL_ADAPTER_RESULT_SECRET"

    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            return {"unexpected": secret}

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
        "failure_phase": "adapter_result",
        "error_type": "MCPAdapterResultType",
        "effect_state": "unknown",
    }
    assert "phase=adapter_result" in outcome.model_text
    assert "error_type=MCPAdapterResultType" in outcome.model_text
    assert secret not in outcome.model_text
    assert secret not in outcome.display_text
    assert secret not in repr(outcome.metadata)


def test_in_flight_tool_transport_loss_is_not_retried() -> None:
    class _Client(MCPClient):
        reconnect_calls = 0

        def is_connected(self) -> bool:
            return True

        async def reconnect(self) -> bool:
            self.reconnect_calls += 1
            return True

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
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


def test_concurrent_reconnect_is_single_flight() -> None:
    class _Client(MCPClient):
        reconnect_once_calls = 0
        gate: asyncio.Event

        async def _reconnect_once(self) -> bool:
            self.reconnect_once_calls += 1
            await self.gate.wait()
            return True

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        client = _Client(config)
        client.gate = asyncio.Event()
        tasks = [asyncio.create_task(client.reconnect()) for _ in range(8)]
        while client.reconnect_once_calls == 0:
            await asyncio.sleep(0)

        assert client.reconnect_once_calls == 1
        client.gate.set()
        assert await asyncio.gather(*tasks) == [True] * 8
        assert client._reconnect_task is None

    asyncio.run(scenario())


def test_reconnect_invalidates_tools_before_transport_renewal() -> None:
    class _Client(MCPClient):
        async def disconnect(self) -> None:
            self.disconnect_started.set()
            await self.disconnect_release.wait()

        async def connect(self) -> bool:
            self._tools = [
                MCPToolInfo(
                    name="renewed",
                    description="renewed",
                    input_schema={"type": "object"},
                    server_name=self.config.name,
                )
            ]
            return True

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        observed: list[tuple[tuple[str, ...] | None, str, str | None]] = []
        client = _Client(
            config,
            on_tools_changed=lambda _client, tools, reason, error_type, _elapsed: (
                observed.append(
                    (
                        tuple(info.name for info in tools)
                        if tools is not None
                        else None,
                        reason,
                        error_type,
                    )
                )
            ),
        )
        client.disconnect_started = asyncio.Event()
        client.disconnect_release = asyncio.Event()
        client._tools = [
            MCPToolInfo(
                name="stale",
                description="stale",
                input_schema={"type": "object"},
                server_name="test",
            )
        ]

        task = asyncio.create_task(client.reconnect())
        await client.disconnect_started.wait()
        assert client.tools == []
        assert observed == [(None, "renew", None)]

        client.disconnect_release.set()
        assert await task is True
        assert observed[-1] == (("renewed",), "renew", None)

    asyncio.run(scenario())


def test_receive_eof_reports_safe_transport_state_once() -> None:
    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        observed: list[tuple[MCPClient, str]] = []
        client = MCPClient(
            config,
            on_transport_closed=lambda current, error_type: observed.append(
                (current, error_type)
            ),
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        client._reader = reader
        client._initialized = True

        await client._receive_loop()

        assert client._initialized is False
        assert observed == [(client, "TransportEOF")]

    asyncio.run(scenario())


def test_tools_list_changed_burst_is_coalesced_and_publishes_final_snapshot() -> None:
    class _Client(MCPClient):
        refresh_calls = 0
        first_started: asyncio.Event
        first_release: asyncio.Event

        async def refresh_tools(self) -> tuple[MCPToolInfo, ...]:
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                self.first_started.set()
                await self.first_release.wait()
            tools = (
                MCPToolInfo(
                    name=f"tool_{self.refresh_calls}",
                    description="dynamic",
                    input_schema={"type": "object"},
                    server_name=self.config.name,
                ),
            )
            self._tools = list(tools)
            return tools

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        observed: list[tuple[tuple[str, ...] | None, str | None]] = []
        client = _Client(
            config,
            on_tools_changed=lambda _client, tools, _reason, error_type, _elapsed: (
                observed.append(
                    (
                        tuple(info.name for info in tools)
                        if tools is not None
                        else None,
                        error_type,
                    )
                )
            ),
        )
        client.first_started = asyncio.Event()
        client.first_release = asyncio.Event()

        client._queue_tools_refresh()
        await client.first_started.wait()
        for _ in range(8):
            client._queue_tools_refresh()
        client.first_release.set()
        task = client._tools_refresh_task
        assert task is not None
        await task

        assert client.refresh_calls == 2
        assert observed[0] == (None, None)
        assert observed[-1] == (("tool_2",), None)
        assert [info.name for info in client.tools] == ["tool_2"]

    asyncio.run(scenario())


def test_receive_loop_recognizes_tools_list_changed_notification() -> None:
    class _Client(MCPClient):
        refresh_notifications = 0

        def _queue_tools_refresh(self) -> None:
            self.refresh_notifications += 1

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        client = _Client(config)
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
        )
        reader.feed_eof()
        client._reader = reader

        await client._receive_loop()

        assert client.refresh_notifications == 1

    asyncio.run(scenario())


def test_tools_refresh_failure_is_observed_without_unhandled_task_error() -> None:
    class _Client(MCPClient):
        async def refresh_tools(self) -> tuple[MCPToolInfo, ...]:
            raise ValueError("catalog unavailable")

    async def scenario() -> None:
        config = SimpleNamespace(name="test", command="test", args=[], env={}, cwd=None)
        observed: list[tuple[tuple[MCPToolInfo, ...] | None, str | None]] = []
        client = _Client(
            config,
            on_tools_changed=lambda _client, tools, _reason, error_type, _elapsed: (
                observed.append((tools, error_type))
            ),
        )

        client._queue_tools_refresh()
        task = client._tools_refresh_task
        assert task is not None
        await task
        await asyncio.sleep(0)

        assert observed == [(None, None), (None, "ValueError")]
        assert task.exception() is None
        assert client.tools == []

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
    assert outcome.metadata == {
        "failure_phase": "request_wait",
        "error_type": "MCPToolRequestCancelled",
        "effect_state": "unknown",
        "mcp_request_id": 42,
    }
    assert "error_type=MCPToolRequestCancelled" in outcome.model_text
    assert "do not blindly repeat" in outcome.model_text


def test_mcp_adapter_reports_lost_in_flight_result_as_unknown() -> None:
    secret = "SENTINEL_TRANSPORT_SECRET"

    class _Client:
        async def call_tool(self, *_args, **_kwargs):
            raise MCPRequestTransportLost(secret, request_id=17)

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
        "failure_phase": "transport",
        "error_type": "MCPRequestTransportLost",
        "effect_state": "unknown",
        "mcp_request_id": 17,
    }
    assert "error_type=MCPRequestTransportLost" in outcome.model_text
    assert "operation may have completed" in outcome.model_text
    assert secret not in outcome.model_text
    assert secret not in outcome.display_text
    assert secret not in repr(outcome.metadata)
