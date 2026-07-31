"""MCP manager - manages MCP servers and tool aggregation."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import threading
import time
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import MCPServerConfig
    from reuleauxcoder.interfaces.events import UIEventBus

from reuleauxcoder.extensions.mcp.adapter import MCPTool
from reuleauxcoder.extensions.mcp.client import MCPClient
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


class MCPManager:
    """Manages connections to multiple MCP servers and aggregates their tools."""

    _CONNECT_TIMEOUT_SECONDS = 30.0

    def __init__(self, ui_bus: "UIEventBus | None" = None):
        self._ui_bus = ui_bus
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPTool] = []
        self._server_tools: dict[str, tuple[MCPTool, ...]] = {}
        self._active_servers: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._state_lock = threading.RLock()
        self._initial_ready = threading.Event()
        self._initial_future: Future[None] | None = None
        self._initial_task: asyncio.Task[None] | None = None
        self._initial_server_order: tuple[str, ...] = ()
        self._initial_failures: set[str] = set()
        self._suppressed_servers: set[str] = set()
        self._initial_state = "idle"
        self._initial_outcome = "ready"
        self._initial_sealed = False
        self.performance_monitor: RuntimePerformanceMonitor | None = None

    def start(self):
        if self._started:
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started = True

        loop = self._loop
        while not loop.is_running():
            time.sleep(0.01)

    def _run_loop(self):
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def stop(self):
        if self._loop and self._loop.is_running():
            self.disconnect_all()
        if self._loop and self._loop.is_running():
            # Schedule stop on the loop
            self._loop.call_soon_threadsafe(self._loop.stop)

        # Wait for thread to finish
        if self._thread:
            self._thread.join(timeout=2.0)

        # Properly close the loop to avoid __del__ errors
        if self._loop and not self._loop.is_running():
            try:
                # Close async generators and shutdown default executor
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.close()
            except Exception:
                pass

        self._loop = None
        self._thread = None
        self._started = False

    @property
    def tools(self) -> list[MCPTool]:
        with self._state_lock:
            return list(self._tools)

    @property
    def connected_servers(self) -> set[str]:
        with self._state_lock:
            return set(self._clients.keys())

    @property
    def active_servers(self) -> set[str]:
        with self._state_lock:
            return set(self._active_servers)

    @property
    def available_tool_count(self) -> int:
        """Return tools ready for the current or pending sealed catalog."""
        with self._state_lock:
            if self._initial_sealed:
                return len(self._tools)
            return sum(
                len(self._server_tools.get(server_name, ()))
                for server_name in self._initial_server_order
                if server_name not in self._suppressed_servers
            )

    @property
    def initial_state(self) -> str:
        with self._state_lock:
            return self._initial_state

    def _emit(self, level: str, message: str) -> None:
        if self._ui_bus is None:
            return
        from reuleauxcoder.interfaces.events import UIEventKind

        emit = getattr(self._ui_bus, level, None)
        if callable(emit):
            emit(message, kind=UIEventKind.MCP)

    def connect_servers_async(self, configs: Iterable["MCPServerConfig"]) -> None:
        """Discover initial MCP tools without blocking interface startup."""
        enabled = tuple(config for config in configs if getattr(config, "enabled", True))
        with self._state_lock:
            if self._initial_state != "idle":
                return
            self._initial_server_order = tuple(config.name for config in enabled)
            self._initial_state = "connecting" if enabled else "ready"
            self._initial_outcome = "ready"
            self._initial_failures.clear()
            self._initial_sealed = False
            self._initial_ready.clear()
        if not enabled:
            self._initial_ready.set()
            return

        self.start()
        assert self._loop is not None
        self._initial_future = asyncio.run_coroutine_threadsafe(
            self._run_initial_discovery(enabled), self._loop
        )

    async def _run_initial_discovery(
        self, configs: tuple["MCPServerConfig", ...]
    ) -> None:
        task = asyncio.current_task()
        self._initial_task = task
        try:
            await self._connect_initial_servers(configs)
        finally:
            if self._initial_task is task:
                self._initial_task = None

    async def _connect_initial_servers(
        self, configs: tuple["MCPServerConfig", ...]
    ) -> None:
        try:
            await asyncio.gather(
                *(self._connect_initial_server(config) for config in configs)
            )
        except asyncio.CancelledError:
            raise
        finally:
            with self._state_lock:
                if not self._initial_sealed:
                    outcome = "degraded" if self._initial_failures else "ready"
                    self._initial_outcome = outcome
                    self._initial_state = outcome
            self._initial_ready.set()
        with self._state_lock:
            connected = len(self._clients)
            failures = len(self._initial_failures)
            sealed_early = self._initial_sealed
        if sealed_early:
            self._emit(
                "warning",
                "MCP startup discovery finished after the tool catalog was sealed; "
                "late tools were not activated.",
            )
        elif failures:
            self._emit(
                "warning",
                f"MCP discovery completed with {connected} connected and "
                f"{failures} failed server(s).",
            )
        else:
            self._emit("success", f"MCP discovery ready ({connected} server(s)).")

    async def _connect_initial_server(self, config: "MCPServerConfig") -> None:
        started = time.monotonic()
        error_type: str | None = None
        client = MCPClient(config, ui_bus=self._ui_bus)
        try:
            success = await asyncio.wait_for(
                client.connect(), timeout=self._CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            await client.disconnect()
            raise
        except Exception as error:
            error_type = type(error).__name__
            success = False
        try:
            if not success:
                await client.disconnect()
                with self._state_lock:
                    self._initial_failures.add(config.name)
                return

            assert self._loop is not None
            tools = tuple(MCPTool(client, info, self._loop) for info in client.tools)
            with self._state_lock:
                suppressed = config.name in self._suppressed_servers
            if suppressed:
                await client.disconnect()
                return
            with self._state_lock:
                self._clients[config.name] = client
                self._server_tools[config.name] = tools
        finally:
            monitor = self.performance_monitor
            if monitor is not None:
                monitor.record(
                    "mcp",
                    "initial_server_connect",
                    (time.monotonic() - started) * 1000,
                    status="ok" if success else "error",
                    attributes={
                        "server_name": config.name,
                        "tool_count": len(client.tools) if success else 0,
                        "error_type": error_type,
                    },
                )

    def seal_initial_catalog(
        self, cancellation_event: threading.Event | None = None
    ) -> tuple[list[MCPTool], str]:
        """Freeze the initial tool catalog exactly once before first inference."""
        started = time.monotonic()
        with self._state_lock:
            if self._initial_sealed:
                tools = list(self._tools)
                outcome = self._initial_outcome
                monitor = self.performance_monitor
                if monitor is not None:
                    monitor.record(
                        "mcp",
                        "catalog_seal_wait",
                        (time.monotonic() - started) * 1000,
                        attributes={
                            "tool_count": len(tools),
                            "outcome": outcome,
                            "already_sealed": True,
                        },
                    )
                return tools, outcome
            state = self._initial_state
        if state == "connecting":
            self._emit("info", "Waiting for initial MCP tool discovery...")
        while state == "connecting" and not self._initial_ready.wait(0.05):
            if cancellation_event is not None and cancellation_event.is_set():
                with self._state_lock:
                    self._initial_outcome = "degraded"
                break
            with self._state_lock:
                state = self._initial_state

        with self._state_lock:
            if not self._initial_sealed:
                self._tools = [
                    tool
                    for server_name in self._initial_server_order
                    for tool in self._server_tools.get(server_name, ())
                ]
                self._active_servers = {
                    server_name
                    for server_name in self._initial_server_order
                    if server_name in self._server_tools
                }
                self._initial_sealed = True
                self._initial_state = "sealed"
            tools = list(self._tools)
            outcome = self._initial_outcome
        self._emit(
            "info",
            f"MCP tool catalog sealed with {len(tools)} tool(s) ({outcome}).",
        )
        monitor = self.performance_monitor
        if monitor is not None:
            monitor.record(
                "mcp",
                "catalog_seal_wait",
                (time.monotonic() - started) * 1000,
                status="ok" if outcome == "ready" else "degraded",
                attributes={
                    "tool_count": len(tools),
                    "outcome": outcome,
                    "already_sealed": False,
                },
            )
        return tools, outcome

    def connect_server(self, config: "MCPServerConfig") -> bool:
        if not self._started:
            self.start()

        if getattr(config, "enabled", True) is False:
            return False

        with self._state_lock:
            existing = self._clients.get(config.name)
            if existing is not None:
                self._suppressed_servers.discard(config.name)
                tools = self._server_tools.get(config.name, ())
                if self._initial_sealed:
                    active_ids = {id(tool) for tool in self._tools}
                    self._tools.extend(
                        tool for tool in tools if id(tool) not in active_ids
                    )
                    self._active_servers.add(config.name)
                elif config.name not in self._initial_server_order:
                    self._initial_server_order = (
                        *self._initial_server_order,
                        config.name,
                    )
                return True

        client = MCPClient(config, ui_bus=self._ui_bus)
        loop = self._loop
        if loop is None:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._connect_runtime_client(client), loop
        )
        try:
            success = future.result(timeout=self._CONNECT_TIMEOUT_SECONDS)
        except Exception as e:
            future.cancel()
            if self._ui_bus:
                from reuleauxcoder.interfaces.events import UIEventKind

                self._ui_bus.error(f"Connection error: {e}", kind=UIEventKind.MCP)
            return False

        if success:
            tools = tuple(MCPTool(client, info, loop) for info in client.tools)
            with self._state_lock:
                self._suppressed_servers.discard(config.name)
                self._clients[config.name] = client
                self._server_tools[config.name] = tools
                if self._initial_sealed:
                    self._tools.extend(tools)
                    self._active_servers.add(config.name)
                elif config.name not in self._initial_server_order:
                    self._initial_server_order = (
                        *self._initial_server_order,
                        config.name,
                    )
        else:
            future = asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            try:
                future.result(timeout=5.0)
            except Exception:
                pass

        return success

    async def _connect_runtime_client(self, client: MCPClient) -> bool:
        try:
            return await client.connect()
        except asyncio.CancelledError:
            await client.disconnect()
            raise

    def disconnect_server(self, server_name: str) -> bool:
        if not self._loop:
            return False

        with self._state_lock:
            self._suppressed_servers.add(server_name)
            client = self._clients.get(server_name)
        if client is None:
            return server_name in self._initial_server_order

        future = asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop)
        try:
            future.result(timeout=5.0)
        except Exception:
            pass

        with self._state_lock:
            self._clients.pop(server_name, None)
            self._server_tools.pop(server_name, None)
            self._active_servers.discard(server_name)
            self._tools = [
                tool
                for tool in self._tools
                if getattr(tool, "server_name", None) != server_name
            ]
        return True

    def disconnect_all(self):
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _disconnect():
            initial_task = self._initial_task
            if initial_task is not None and initial_task is not asyncio.current_task():
                initial_task.cancel()
                await asyncio.gather(initial_task, return_exceptions=True)
            with self._state_lock:
                clients = tuple(self._clients.values())
            await asyncio.gather(
                *(client.disconnect() for client in clients),
                return_exceptions=True,
            )

        future = asyncio.run_coroutine_threadsafe(_disconnect(), loop)
        try:
            future.result(timeout=5.0)
        except Exception:
            pass

        with self._state_lock:
            self._clients.clear()
            self._server_tools.clear()
            self._tools.clear()
            self._active_servers.clear()
            self._suppressed_servers.clear()
        self._initial_future = None

    def get_tool(self, name: str) -> MCPTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None
