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

    def __init__(
        self,
        config,
        ui_bus=None,
        *,
        on_transport_closed=None,
        on_tools_changed=None,
    ) -> None:
        self.config = config
        self.tools: list[MCPToolInfo] = []
        self.disconnected = False
        self.on_transport_closed = on_transport_closed
        self.on_tools_changed = on_tools_changed

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
        assert [tool.name for tool in manager._server_tools["race"]] == ["race_tool"]
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


def test_dynamic_capability_snapshot_replaces_agent_catalog(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    published: list[tuple[str, ...]] = []
    manager = MCPManager()
    manager.bind_runtime_observers(
        catalog_listener=lambda tools: published.append(
            tuple(tool.name for tool in tools)
        )
    )
    manager.performance_monitor = RuntimePerformanceMonitor()
    server = _server("dynamic")

    try:
        assert manager.connect_server(server)
        manager.seal_initial_catalog()
        client = manager._clients["dynamic"]
        generation = manager.runtime_statuses[0].generation
        replacement = (
            MCPToolInfo(
                name="replacement",
                description="new tool",
                input_schema={"type": "object"},
                server_name="dynamic",
            ),
        )

        client.on_tools_changed(
            client,
            None,
            "list_changed",
            None,
            0.0,
        )
        assert manager.runtime_statuses[0].state is MCPRuntimeState.REFRESHING
        assert manager.tools == []
        assert published[-1] == ()

        client.on_tools_changed(
            client,
            replacement,
            "list_changed",
            None,
            12.5,
        )

        status = manager.runtime_statuses[0]
        assert status.generation == generation
        assert status.state is MCPRuntimeState.CONNECTED
        assert status.tool_count == 1
        assert [tool.name for tool in manager.tools] == ["replacement"]
        assert published[-1] == ("replacement",)
        assert manager.catalog_generation == 3
        sample = manager.performance_monitor.snapshot(category="mcp")[-1]
        assert sample.name == "capability_refresh"
        assert sample.status == "ok"
        assert sample.attribute_map() == {
            "catalog_generation": 3,
            "error_type": None,
            "generation": generation,
            "old_tool_count": 0,
            "reason": "list_changed",
            "server_name": "dynamic",
            "tool_count": 1,
        }
    finally:
        manager.disconnect_all()
        manager.stop()


def test_dynamic_refresh_failure_removes_stale_tools_and_informs_agent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    published: list[tuple[str, ...]] = []
    issues: list[tuple[str, str, str]] = []
    manager = MCPManager()
    manager.bind_runtime_observers(
        catalog_listener=lambda tools: published.append(
            tuple(tool.name for tool in tools)
        ),
        runtime_issue_sink=lambda phase, error_type, ref: issues.append(
            (phase, error_type, ref)
        ),
    )
    manager.performance_monitor = RuntimePerformanceMonitor()

    try:
        assert manager.connect_server(_server("failing-server"))
        manager.seal_initial_catalog()
        client = manager._clients["failing-server"]
        client.on_tools_changed(
            client,
            None,
            "list_changed",
            "MCPRequestTimeout",
            31.0,
        )

        status = manager.runtime_statuses[0]
        assert status.state is MCPRuntimeState.ERROR
        assert status.error_type == "MCPRequestTimeout"
        assert status.tool_count == 0
        assert manager.tools == []
        assert published[-1] == ()
        assert issues == [
            (
                "mcp_capability_refresh",
                "MCPRequestTimeout",
                "server_failing_server",
            )
        ]
        sample = manager.performance_monitor.snapshot(category="mcp")[-1]
        assert sample.name == "capability_refresh"
        assert sample.status == "error"
        assert sample.attribute_map()["error_type"] == "MCPRequestTimeout"
    finally:
        manager.disconnect_all()
        manager.stop()


def test_old_generation_capability_callback_cannot_pollute_current_catalog(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    manager = MCPManager()

    try:
        server = _server("stale")
        assert manager.connect_server(server)
        manager.seal_initial_catalog()
        old = manager._clients["stale"]
        assert manager.disconnect_server("stale")
        assert manager.connect_server(server)
        current_generation = manager.runtime_statuses[0].generation
        before = [tool.name for tool in manager.tools]

        old.on_tools_changed(
            old,
            (
                MCPToolInfo(
                    name="stale_result",
                    description="must be discarded",
                    input_schema={"type": "object"},
                    server_name="stale",
                ),
            ),
            "list_changed",
            None,
            5.0,
        )

        assert manager.runtime_statuses[0].generation == current_generation
        assert [tool.name for tool in manager.tools] == before
        assert "stale_result" not in before
    finally:
        manager.disconnect_all()
        manager.stop()


def test_transport_eof_publishes_empty_catalog_and_runtime_issue(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "MCPClient", _FakeClient)
    _FakeClient.delays = {}
    _FakeClient.failures = set()
    published: list[tuple[str, ...]] = []
    issues: list[tuple[str, str, str]] = []
    manager = MCPManager()
    manager.bind_runtime_observers(
        catalog_listener=lambda tools: published.append(
            tuple(tool.name for tool in tools)
        ),
        runtime_issue_sink=lambda *issue: issues.append(issue),
    )

    try:
        assert manager.connect_server(_server("eof"))
        manager.seal_initial_catalog()
        client = manager._clients["eof"]

        client.on_transport_closed(client, "TransportEOF")

        assert manager.tools == []
        assert published[-1] == ()
        assert issues == [("mcp_transport", "TransportEOF", "server_eof")]
    finally:
        manager.disconnect_all()
        manager.stop()


def test_disconnect_cleanup_failure_is_visible_and_not_reported_as_success(
    monkeypatch,
) -> None:
    class _BrokenDisconnectClient(_FakeClient):
        async def disconnect(self) -> None:
            self.disconnected = True
            raise OSError("cleanup failed")

    monkeypatch.setattr(manager_module, "MCPClient", _BrokenDisconnectClient)
    _BrokenDisconnectClient.delays = {}
    _BrokenDisconnectClient.failures = set()
    issues: list[tuple[str, str, str]] = []
    manager = MCPManager()
    manager.bind_runtime_observers(
        runtime_issue_sink=lambda *issue: issues.append(issue)
    )
    manager.performance_monitor = RuntimePerformanceMonitor()

    try:
        assert manager.connect_server(_server("cleanup"))

        assert manager.disconnect_server("cleanup") is False

        status = manager.runtime_statuses[0]
        assert status.state is MCPRuntimeState.SUPPRESSED
        assert status.error_type == "OSError"
        assert manager.tools == []
        assert issues == [("mcp_disconnect_cleanup", "OSError", "server_cleanup")]
        sample = manager.performance_monitor.snapshot(category="mcp")[-1]
        assert sample.name == "runtime_disconnect"
        assert sample.status == "error"
    finally:
        manager.disconnect_all()
        manager.stop()
