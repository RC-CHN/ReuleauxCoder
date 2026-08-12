import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.extensions.mcp import manager as manager_module
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.mcp.models import MCPRuntimeState
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


class _FakeClient:
    delays: dict[str, float] = {}
    failures: set[str] = set()

    def __init__(self, config, ui_bus=None, *, on_transport_closed=None) -> None:
        self.config = config
        self.tools: list[MCPToolInfo] = []
        self.disconnected = False
        self.on_transport_closed = on_transport_closed

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

    def is_connected(self) -> bool:
        return not self.disconnected


class _ControlledClient(_FakeClient):
    created: list["_ControlledClient"] = []
    gates: list[asyncio.Event] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.index = len(self.created)
        self.created.append(self)

    async def connect(self) -> bool:
        gate = self.gates[self.index]
        await gate.wait()
        return await super().connect()


def _server(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, command="fake")


def test_initial_discovery_is_nonblocking_and_seals_in_config_order(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {"first": 0.12, "second": 0.01}
    _FakeClient.failures = set()
    manager = MCPManager()
    manager.performance_monitor = RuntimePerformanceMonitor()

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
        samples = manager.performance_monitor.snapshot()
        assert [sample.name for sample in samples].count("initial_server_connect") == 2
        assert samples[-1].name == "catalog_seal_wait"
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


def test_initial_adapter_failure_is_degraded_and_observable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()

    def fail_adapter(*_args, **_kwargs):
        raise ValueError("credential=adapter-construction-secret")

    monkeypatch.setattr(manager_module, "MCPTool", fail_adapter)
    manager = MCPManager()

    try:
        manager.connect_servers_async([_server("broken-adapter")])

        tools, outcome = manager.seal_initial_catalog()

        assert tools == []
        assert outcome == "degraded"
        assert manager.connected_servers == set()
        status = manager.runtime_statuses[0]
        assert status.state is MCPRuntimeState.ERROR
        assert status.error_type == "ValueError"
        assert "credential=" not in repr(status)
    finally:
        manager.disconnect_all()
        manager.stop()


def test_stale_initial_connect_cannot_overwrite_reenabled_generation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _ControlledClient)
    _ControlledClient.created = []
    _ControlledClient.gates = [asyncio.Event(), asyncio.Event()]
    _ControlledClient.delays = {}
    _ControlledClient.failures = set()
    manager = MCPManager()
    server = _server("race")

    try:
        manager.connect_servers_async([server])
        while len(_ControlledClient.created) < 1:
            time.sleep(0.01)
        first = _ControlledClient.created[0]
        assert manager.disconnect_server("race") is True

        result: list[bool] = []
        thread = threading.Thread(
            target=lambda: result.append(manager.connect_server(server))
        )
        thread.start()
        while len(_ControlledClient.created) < 2:
            time.sleep(0.01)
        second = _ControlledClient.created[1]
        manager._loop.call_soon_threadsafe(_ControlledClient.gates[1].set)
        thread.join(timeout=2)
        assert result == [True]
        reenabled = manager.runtime_statuses[0]
        assert reenabled.state is MCPRuntimeState.CONNECTED
        assert reenabled.generation == 3

        manager._loop.call_soon_threadsafe(_ControlledClient.gates[0].set)
        assert manager._initial_ready.wait(timeout=2)
        time.sleep(0.05)

        current = manager.runtime_statuses[0]
        assert current == reenabled
        assert manager._clients["race"] is second
        assert first.disconnected is True
        assert [tool.name for tool in manager._server_tools["race"]] == [
            "race_tool"
        ]
    finally:
        manager.disconnect_all()
        manager.stop()


def test_runtime_connect_is_single_flight_per_manager(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _ControlledClient)
    _ControlledClient.created = []
    _ControlledClient.gates = [asyncio.Event()]
    _ControlledClient.delays = {}
    _ControlledClient.failures = set()
    manager = MCPManager()
    server = _server("single")

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(manager.connect_server, server) for _ in range(8)]
            while len(_ControlledClient.created) < 1:
                time.sleep(0.01)
            time.sleep(0.05)
            assert len(_ControlledClient.created) == 1
            manager._loop.call_soon_threadsafe(_ControlledClient.gates[0].set)
            assert [future.result(timeout=2) for future in futures] == [True] * 8

        assert len(_ControlledClient.created) == 1
        assert manager.runtime_statuses[0].generation == 1
    finally:
        manager.disconnect_all()
        manager.stop()


def test_runtime_generations_are_manager_scoped(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    first = MCPManager()
    second = MCPManager()
    server = _server("isolated")

    try:
        assert first.connect_server(server)
        assert first.disconnect_server("isolated")
        assert first.connect_server(server)
        assert second.connect_server(server)

        assert first.runtime_statuses[0].generation == 3
        assert second.runtime_statuses[0].generation == 1
    finally:
        first.disconnect_all()
        first.stop()
        second.disconnect_all()
        second.stop()


def test_current_transport_eof_removes_false_connected_capability(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    manager = MCPManager()
    server = _server("closed")

    try:
        assert manager.connect_server(server)
        status = manager.runtime_statuses[0]
        client = manager._clients["closed"]

        client.on_transport_closed(client, "TransportEOF")

        failed = manager.runtime_statuses[0]
        assert failed.generation == status.generation
        assert failed.state is MCPRuntimeState.ERROR
        assert failed.error_type == "TransportEOF"
        assert failed.tool_count == 0
        assert manager.connected_servers == set()
        assert manager.active_servers == set()
        assert manager.tools == []
    finally:
        manager.disconnect_all()
        manager.stop()


def test_runtime_connect_disconnect_performance_has_generation_and_outcome(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    manager = MCPManager()
    manager.performance_monitor = RuntimePerformanceMonitor()
    server = _server("observed")

    try:
        assert manager.connect_server(server)
        assert manager.disconnect_server("observed")

        samples = manager.performance_monitor.snapshot(category="mcp")
        assert [sample.name for sample in samples] == [
            "runtime_connect",
            "runtime_disconnect",
        ]
        assert [sample.status for sample in samples] == ["ok", "ok"]
        assert samples[0].attribute_map() == {
            "error_type": None,
            "generation": 1,
            "server_name": "observed",
            "tool_count": 1,
        }
        assert samples[1].attribute_map() == {
            "error_type": None,
            "generation": 2,
            "server_name": "observed",
            "tool_count": 0,
        }
    finally:
        manager.disconnect_all()
        manager.stop()
