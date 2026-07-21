import asyncio
import threading
import time

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.extensions.mcp import manager as manager_module
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.mcp.models import MCPToolInfo


class _FakeClient:
    delays: dict[str, float] = {}
    failures: set[str] = set()

    def __init__(self, config, ui_bus=None) -> None:
        self.config = config
        self.tools: list[MCPToolInfo] = []
        self.disconnected = False

    async def connect(self) -> bool:
        await asyncio.sleep(self.delays.get(self.config.name, 0.0))
        if self.config.name in self.failures:
            return False
        self.tools = [
            MCPToolInfo(
                name=f"{self.config.name}_tool",
                description=f"Tool from {self.config.name}",
                input_schema={"type": "object", "properties": {}},
                server_name=self.config.name,
            )
        ]
        return True

    async def disconnect(self) -> None:
        self.disconnected = True


def _server(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, command="fake")


def test_initial_discovery_is_nonblocking_and_seals_in_config_order(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {"first": 0.12, "second": 0.01}
    _FakeClient.failures = set()
    manager = MCPManager()

    try:
        started = time.monotonic()
        manager.connect_servers_async([_server("first"), _server("second")])
        startup_elapsed = time.monotonic() - started

        tools, outcome = manager.seal_initial_catalog()

        assert startup_elapsed < 0.08
        assert outcome == "ready"
        assert [tool.name for tool in tools] == ["first_tool", "second_tool"]
        assert manager.available_tool_count == 2
        assert manager.active_servers == {"first", "second"}
        assert manager.initial_state == "sealed"
    finally:
        manager.disconnect_all()
        manager.stop()


def test_cancelling_first_request_seals_partial_catalog_promptly(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {"slow": 1.0}
    _FakeClient.failures = set()
    manager = MCPManager()
    cancellation = threading.Event()

    try:
        manager.connect_servers_async([_server("slow")])
        timer = threading.Timer(0.05, cancellation.set)
        timer.start()
        started = time.monotonic()

        tools, outcome = manager.seal_initial_catalog(cancellation)

        assert time.monotonic() - started < 0.4
        assert tools == []
        assert outcome == "degraded"
        assert manager.initial_state == "sealed"
    finally:
        manager.disconnect_all()
        manager.stop()


def test_failed_server_does_not_prevent_ready_tools_from_being_sealed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = {"broken"}
    manager = MCPManager()

    try:
        manager.connect_servers_async([_server("broken"), _server("working")])

        tools, outcome = manager.seal_initial_catalog()

        assert outcome == "degraded"
        assert [tool.name for tool in tools] == ["working_tool"]
        assert manager.connected_servers == {"working"}
        assert manager.active_servers == {"working"}
    finally:
        manager.disconnect_all()
        manager.stop()


def test_initial_discovery_timeout_degrades_without_blocking_forever(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {"slow": 1.0}
    _FakeClient.failures = set()
    manager = MCPManager()
    manager._CONNECT_TIMEOUT_SECONDS = 0.05

    try:
        manager.connect_servers_async([_server("slow")])
        started = time.monotonic()

        tools, outcome = manager.seal_initial_catalog()

        assert time.monotonic() - started < 0.4
        assert tools == []
        assert outcome == "degraded"
    finally:
        manager.stop()
