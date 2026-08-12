"""MCP manager - manages MCP servers and tool aggregation."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import MCPServerConfig
    from reuleauxcoder.interfaces.events import UIEventBus

from reuleauxcoder.extensions.mcp.adapter import MCPTool
from reuleauxcoder.extensions.mcp.client import MCPClient
from reuleauxcoder.extensions.mcp.models import (
    MCPRuntimeState,
    MCPRuntimeStatus,
    MCPToolInfo,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


@dataclass(slots=True)
class _MCPRuntimeSlot:
    name: str
    state: MCPRuntimeState = MCPRuntimeState.UNSTARTED
    generation: int = 0
    client: MCPClient | None = None
    tools: tuple[MCPTool, ...] = ()
    in_flight: Future[bool] | None = field(default=None, repr=False)
    last_error_type: str | None = None


class MCPManager:
    """Manages connections to multiple MCP servers and aggregates their tools."""

    _CONNECT_TIMEOUT_SECONDS = 30.0
    _LOOP_WAKEUP_FALLBACK_SECONDS = 0.01

    def __init__(self, ui_bus: "UIEventBus | None" = None):
        self._ui_bus = ui_bus
        self._clients: dict[str, MCPClient] = {}
        self._tools: list[MCPTool] = []
        self._server_tools: dict[str, tuple[MCPTool, ...]] = {}
        self._active_servers: set[str] = set()
        self._runtime_slots: dict[str, _MCPRuntimeSlot] = {}
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
        self._catalog_generation = 0
        self._catalog_listener: Callable[[tuple[MCPTool, ...]], object] | None = None
        self._runtime_issue_sink: Callable[[str, str, str], object] | None = None
        self.performance_monitor: RuntimePerformanceMonitor | None = None

    def start(self):
        with self._state_lock:
            if not self._started:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._run_loop, daemon=True)
                self._started = True
                self._thread.start()
            loop = self._loop
        assert loop is not None
        while not loop.is_running():
            time.sleep(0.01)

    def _run_loop(self):
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)

        # Some restricted runtimes deny writes to asyncio's internal
        # socketpair. A bounded no-op tick keeps thread-safe submissions and
        # stop requests observable without changing MCP operation ordering.
        def wakeup_fallback() -> None:
            if loop.is_running():
                loop.call_later(
                    self._LOOP_WAKEUP_FALLBACK_SECONDS,
                    wakeup_fallback,
                )

        loop.call_later(self._LOOP_WAKEUP_FALLBACK_SECONDS, wakeup_fallback)
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
            return {
                name
                for name, slot in self._runtime_slots.items()
                if slot.state is MCPRuntimeState.CONNECTED
                and slot.client is not None
                and self._client_connected(slot.client)
            }

    @property
    def active_servers(self) -> set[str]:
        with self._state_lock:
            return set(self._active_servers)

    @property
    def catalog_generation(self) -> int:
        with self._state_lock:
            return self._catalog_generation

    @property
    def runtime_statuses(self) -> tuple[MCPRuntimeStatus, ...]:
        """Return one immutable snapshot without probing or reconnecting."""
        with self._state_lock:
            return tuple(
                MCPRuntimeStatus(
                    server_name=slot.name,
                    state=slot.state,
                    generation=slot.generation,
                    tool_count=len(slot.tools),
                    error_type=slot.last_error_type,
                )
                for slot in sorted(
                    self._runtime_slots.values(), key=lambda item: item.name
                )
            )

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

    def bind_runtime_observers(
        self,
        *,
        catalog_listener: Callable[[tuple[MCPTool, ...]], object] | None = None,
        runtime_issue_sink: Callable[[str, str, str], object] | None = None,
    ) -> None:
        """Bind owner-scoped sinks for dynamic catalog and failure facts."""
        with self._state_lock:
            self._catalog_listener = catalog_listener
            self._runtime_issue_sink = runtime_issue_sink

    @staticmethod
    def _safe_runtime_ref(server_name: str) -> str:
        suffix = "".join(
            character if character.isascii() and character.isalnum() else "_"
            for character in server_name
        ).strip("_")[:48]
        return f"server_{suffix or 'unknown'}"

    def _record_runtime_issue(
        self,
        phase: str,
        error_type: str,
        server_name: str,
    ) -> None:
        sink = self._runtime_issue_sink
        if sink is None:
            return
        try:
            sink(
                phase,
                self._safe_error_type(error_type).replace("-", "_").replace(".", "_"),
                self._safe_runtime_ref(server_name),
            )
        except Exception as error:
            self._emit(
                "error",
                "MCP runtime-issue observer failed "
                f"(error_type={self._safe_error_type(error)}).",
            )

    def _publish_catalog_snapshot(
        self,
        tools: tuple[MCPTool, ...],
        *,
        server_name: str,
    ) -> None:
        listener = self._catalog_listener
        if listener is None:
            return
        try:
            listener(tools)
        except Exception as error:
            error_type = self._safe_error_type(error)
            self._emit(
                "error",
                f"MCP catalog observer failed (error_type={error_type}).",
            )
            self._record_runtime_issue(
                "mcp_catalog_observer",
                error_type,
                server_name,
            )

    def _catalog_changed_locked(self) -> tuple[tuple[MCPTool, ...], int] | None:
        if not self._initial_sealed:
            return None
        self._catalog_generation += 1
        return tuple(self._tools), self._catalog_generation

    def _emit(self, level: str, message: str) -> None:
        if self._ui_bus is None:
            return
        from reuleauxcoder.interfaces.events import UIEventKind

        emit = getattr(self._ui_bus, level, None)
        if callable(emit):
            emit(message, kind=UIEventKind.MCP)

    def _slot_locked(self, server_name: str) -> _MCPRuntimeSlot:
        slot = self._runtime_slots.get(server_name)
        if slot is None:
            slot = _MCPRuntimeSlot(server_name)
            self._runtime_slots[server_name] = slot
        return slot

    @staticmethod
    def _safe_error_type(error: BaseException | str) -> str:
        name = error if isinstance(error, str) else type(error).__name__
        safe = "".join(
            character
            for character in name
            if character.isascii()
            and (character.isalnum() or character in {"_", "-", "."})
        )[:64]
        return safe or "Error"

    @staticmethod
    def _client_connected(client: MCPClient | None) -> bool:
        if client is None:
            return False
        check = getattr(client, "is_connected", None)
        if not callable(check):
            return True
        try:
            return bool(check())
        except Exception:
            return False

    def _rebuild_tools_locked(self) -> None:
        if not self._initial_sealed:
            return
        ordered = list(self._initial_server_order)
        ordered.extend(name for name in self._server_tools if name not in ordered)
        self._tools = [
            tool
            for server_name in ordered
            if server_name in self._active_servers
            for tool in self._server_tools.get(server_name, ())
        ]

    def _remove_server_locked(self, server_name: str) -> None:
        self._clients.pop(server_name, None)
        self._server_tools.pop(server_name, None)
        self._active_servers.discard(server_name)
        self._rebuild_tools_locked()

    def _activate_server_locked(
        self,
        server_name: str,
        client: MCPClient,
        tools: tuple[MCPTool, ...],
        *,
        activate_if_sealed: bool,
    ) -> None:
        self._clients[server_name] = client
        self._server_tools[server_name] = tools
        if self._initial_sealed and activate_if_sealed:
            self._active_servers.add(server_name)
            self._rebuild_tools_locked()
        elif server_name not in self._initial_server_order:
            self._initial_server_order = (*self._initial_server_order, server_name)

    def _new_client(
        self,
        config: "MCPServerConfig",
        generation: int,
    ) -> MCPClient:
        return MCPClient(
            config,
            ui_bus=self._ui_bus,
            on_transport_closed=lambda client, error_type: self._on_transport_closed(
                config.name,
                generation,
                client,
                error_type,
            ),
            on_tools_changed=lambda client, tools, reason, error_type, elapsed_ms: (
                self._on_tools_changed(
                    config.name,
                    generation,
                    client,
                    tools,
                    reason,
                    error_type,
                    elapsed_ms,
                )
            ),
        )

    def _on_tools_changed(
        self,
        server_name: str,
        generation: int,
        client: MCPClient,
        tool_infos: tuple[MCPToolInfo, ...] | None,
        reason: str,
        error_type: str | None,
        elapsed_ms: float,
    ) -> None:
        """Generation-gate and atomically publish one capability change."""
        safe_error = self._safe_error_type(error_type) if error_type else None
        tools: tuple[MCPTool, ...] = ()
        if tool_infos is not None:
            loop = self._loop
            if loop is None:
                safe_error = "RuntimeLoopUnavailable"
                tool_infos = None
            else:
                try:
                    tools = tuple(MCPTool(client, info, loop) for info in tool_infos)
                except Exception as error:
                    safe_error = self._safe_error_type(error)
                    tool_infos = None

        with self._state_lock:
            slot = self._runtime_slots.get(server_name)
            if (
                slot is None
                or slot.generation != generation
                or slot.client is not client
                or server_name in self._suppressed_servers
            ):
                return
            old_count = len(slot.tools)
            if tool_infos is None:
                slot.state = (
                    MCPRuntimeState.ERROR
                    if safe_error is not None
                    else MCPRuntimeState.REFRESHING
                )
                slot.tools = ()
                slot.last_error_type = safe_error
                self._server_tools.pop(server_name, None)
                self._active_servers.discard(server_name)
                self._rebuild_tools_locked()
                new_count = 0
            else:
                slot.state = MCPRuntimeState.CONNECTED
                slot.tools = tools
                slot.last_error_type = None
                self._activate_server_locked(
                    server_name,
                    client,
                    tools,
                    activate_if_sealed=True,
                )
                new_count = len(tools)
            catalog_update = self._catalog_changed_locked()

        if catalog_update is not None:
            snapshot, catalog_generation = catalog_update
            self._publish_catalog_snapshot(snapshot, server_name=server_name)
        else:
            catalog_generation = self._catalog_generation

        if elapsed_ms > 0 or safe_error is not None or tool_infos is not None:
            monitor = self.performance_monitor
            if monitor is not None:
                try:
                    monitor.record(
                        "mcp",
                        "runtime_renew" if reason == "renew" else "capability_refresh",
                        max(0.0, elapsed_ms),
                        status="error" if safe_error is not None else "ok",
                        attributes={
                            "server_name": server_name,
                            "generation": generation,
                            "catalog_generation": catalog_generation,
                            "reason": reason,
                            "old_tool_count": old_count,
                            "tool_count": new_count,
                            "error_type": safe_error,
                        },
                    )
                except Exception as error:
                    monitor_error_type = self._safe_error_type(error)
                    self._emit(
                        "error",
                        "MCP performance observer failed "
                        f"(error_type={monitor_error_type}).",
                    )
                    self._record_runtime_issue(
                        "mcp_performance_observer",
                        monitor_error_type,
                        server_name,
                    )
        if safe_error is not None:
            self._record_runtime_issue(
                "mcp_capability_refresh",
                safe_error,
                server_name,
            )
            self._emit(
                "error",
                f"MCP capability refresh failed (server={server_name}, "
                f"generation={generation}, catalog_generation={catalog_generation}, "
                f"error_type={safe_error}).",
            )
        elif tool_infos is not None:
            self._emit(
                "info",
                f"MCP capability catalog changed (server={server_name}, "
                f"generation={generation}, catalog_generation={catalog_generation}, "
                f"tools={old_count}->{new_count}, reason={reason}).",
            )

    def _on_transport_closed(
        self,
        server_name: str,
        generation: int,
        client: MCPClient,
        error_type: str,
    ) -> None:
        """Invalidate capability only for the current client generation."""
        with self._state_lock:
            slot = self._runtime_slots.get(server_name)
            if (
                slot is None
                or slot.generation != generation
                or slot.client is not client
            ):
                return
            slot.state = MCPRuntimeState.ERROR
            slot.last_error_type = self._safe_error_type(error_type)
            slot.tools = ()
            self._remove_server_locked(server_name)
            catalog_update = self._catalog_changed_locked()
            safe_error = slot.last_error_type
        if catalog_update is not None:
            snapshot, _ = catalog_update
            self._publish_catalog_snapshot(snapshot, server_name=server_name)
        self._record_runtime_issue("mcp_transport", safe_error, server_name)
        self._emit(
            "error",
            f"MCP server '{server_name}' transport closed "
            f"(generation={generation}, error_type={safe_error}).",
        )

    def connect_servers_async(self, configs: Iterable["MCPServerConfig"]) -> None:
        """Discover initial MCP tools without blocking interface startup."""
        enabled = tuple(
            config for config in configs if getattr(config, "enabled", True)
        )
        with self._state_lock:
            if self._initial_state != "idle":
                return
            self._initial_server_order = tuple(config.name for config in enabled)
            self._initial_state = "connecting" if enabled else "ready"
            self._initial_outcome = "ready"
            self._initial_failures.clear()
            self._initial_sealed = False
            self._initial_ready.clear()
            generations: dict[str, int] = {}
            for config in enabled:
                slot = self._slot_locked(config.name)
                slot.generation += 1
                slot.state = MCPRuntimeState.CONNECTING
                slot.last_error_type = None
                slot.in_flight = Future()
                generations[config.name] = slot.generation
        if not enabled:
            self._initial_ready.set()
            return

        self.start()
        assert self._loop is not None
        self._initial_future = asyncio.run_coroutine_threadsafe(
            self._run_initial_discovery(enabled, generations), self._loop
        )

    async def _run_initial_discovery(
        self,
        configs: tuple["MCPServerConfig", ...],
        generations: dict[str, int],
    ) -> None:
        task = asyncio.current_task()
        self._initial_task = task
        try:
            await self._connect_initial_servers(configs, generations)
        finally:
            if self._initial_task is task:
                self._initial_task = None

    async def _connect_initial_servers(
        self,
        configs: tuple["MCPServerConfig", ...],
        generations: dict[str, int],
    ) -> None:
        try:
            await asyncio.gather(
                *(
                    self._connect_initial_server(
                        config,
                        generations[config.name],
                    )
                    for config in configs
                )
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

    async def _connect_initial_server(
        self,
        config: "MCPServerConfig",
        generation: int,
    ) -> None:
        started = time.monotonic()
        error_type: str | None = None
        client: MCPClient | None = None
        success = False
        try:
            client = self._new_client(config, generation)
            success = await asyncio.wait_for(
                client.connect(), timeout=self._CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            if client is not None:
                await client.disconnect()
            raise
        except Exception as error:
            error_type = self._safe_error_type(error)
            success = False
        try:
            if not success:
                if client is not None:
                    await client.disconnect()
                with self._state_lock:
                    self._initial_failures.add(config.name)
                    slot = self._runtime_slots.get(config.name)
                    if slot is not None and slot.generation == generation:
                        slot.state = MCPRuntimeState.ERROR
                        slot.last_error_type = error_type or "ConnectFailed"
                return

            assert client is not None
            assert self._loop is not None
            tools = tuple(MCPTool(client, info, self._loop) for info in client.tools)
            with self._state_lock:
                slot = self._runtime_slots.get(config.name)
                stale = (
                    slot is None
                    or slot.generation != generation
                    or config.name in self._suppressed_servers
                )
            if stale:
                await client.disconnect()
                return
            with self._state_lock:
                slot = self._runtime_slots[config.name]
                if slot.generation != generation:
                    stale = True
                else:
                    stale = False
                    slot.state = MCPRuntimeState.CONNECTED
                    slot.client = client
                    slot.tools = tools
                    slot.last_error_type = None
                    self._activate_server_locked(
                        config.name,
                        client,
                        tools,
                        activate_if_sealed=False,
                    )
            if stale:
                await client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_type = self._safe_error_type(error)
            success = False
            if client is not None:
                try:
                    await client.disconnect()
                except Exception as cleanup_error:
                    self._emit(
                        "error",
                        f"MCP initial cleanup failed (server={config.name}, "
                        f"generation={generation}, "
                        f"error_type={self._safe_error_type(cleanup_error)}).",
                    )
            with self._state_lock:
                self._initial_failures.add(config.name)
                slot = self._runtime_slots.get(config.name)
                if slot is not None and slot.generation == generation:
                    slot.state = MCPRuntimeState.ERROR
                    slot.client = None
                    slot.tools = ()
                    slot.last_error_type = error_type
                    self._remove_server_locked(config.name)
        finally:
            with self._state_lock:
                slot = self._runtime_slots.get(config.name)
                shared = (
                    slot.in_flight
                    if slot is not None and slot.generation == generation
                    else None
                )
                current_success = bool(
                    slot is not None
                    and slot.generation == generation
                    and slot.state is MCPRuntimeState.CONNECTED
                )
            if shared is not None and not shared.done():
                shared.set_result(current_success)
            monitor = self.performance_monitor
            if monitor is not None:
                monitor.record(
                    "mcp",
                    "initial_server_connect",
                    (time.monotonic() - started) * 1000,
                    status="ok" if success else "error",
                    attributes={
                        "server_name": config.name,
                        "tool_count": (
                            len(client.tools) if success and client is not None else 0
                        ),
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
                self._catalog_generation += 1
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

        loop = self._loop
        if loop is None:
            return False

        with self._state_lock:
            slot = self._slot_locked(config.name)
            existing = slot.client
            if (
                slot.state is MCPRuntimeState.CONNECTED
                and existing is not None
                and self._client_connected(existing)
            ):
                self._suppressed_servers.discard(config.name)
                if self._initial_sealed:
                    self._active_servers.add(config.name)
                    self._rebuild_tools_locked()
                elif config.name not in self._initial_server_order:
                    self._initial_server_order = (
                        *self._initial_server_order,
                        config.name,
                    )
                return True
            shared = slot.in_flight
            if shared is None or shared.done():
                previous = existing or self._clients.get(config.name)
                slot.generation += 1
                generation = slot.generation
                slot.state = MCPRuntimeState.CONNECTING
                slot.client = None
                slot.tools = ()
                slot.last_error_type = None
                self._suppressed_servers.discard(config.name)
                self._remove_server_locked(config.name)
                client = self._new_client(config, generation)
                shared = asyncio.run_coroutine_threadsafe(
                    self._connect_runtime_generation(
                        config,
                        generation,
                        client,
                        previous,
                    ),
                    loop,
                )
                slot.in_flight = shared
        try:
            return shared.result(timeout=self._CONNECT_TIMEOUT_SECONDS)
        except Exception as error:
            self._emit(
                "error",
                f"MCP connection failed (server={config.name}, "
                f"error_type={self._safe_error_type(error)}).",
            )
            return False

    async def _connect_runtime_generation(
        self,
        config: "MCPServerConfig",
        generation: int,
        client: MCPClient,
        previous: MCPClient | None,
    ) -> bool:
        started = time.monotonic()
        success = False
        error_type: str | None = None
        try:
            if previous is not None and previous is not client:
                await previous.disconnect()
            success = await client.connect()
        except asyncio.CancelledError:
            await client.disconnect()
            raise
        except Exception as error:
            error_type = self._safe_error_type(error)

        loop = self._loop
        tools = (
            tuple(MCPTool(client, info, loop) for info in client.tools)
            if success and loop is not None
            else ()
        )
        with self._state_lock:
            slot = self._runtime_slots.get(config.name)
            current = (
                slot is not None
                and slot.generation == generation
                and config.name not in self._suppressed_servers
            )
            if current and success:
                slot.state = MCPRuntimeState.CONNECTED
                slot.client = client
                slot.tools = tools
                slot.last_error_type = None
                self._activate_server_locked(
                    config.name,
                    client,
                    tools,
                    activate_if_sealed=True,
                )
                catalog_update = self._catalog_changed_locked()
            elif current:
                slot.state = MCPRuntimeState.ERROR
                slot.client = None
                slot.tools = ()
                slot.last_error_type = error_type or "ConnectFailed"
                self._remove_server_locked(config.name)
                catalog_update = self._catalog_changed_locked()
            else:
                catalog_update = None
        if catalog_update is not None:
            snapshot, _ = catalog_update
            self._publish_catalog_snapshot(snapshot, server_name=config.name)
        if not current or not success:
            try:
                await client.disconnect()
            except Exception as cleanup_error:
                self._emit(
                    "error",
                    f"MCP stale/failed client cleanup failed "
                    f"(server={config.name}, generation={generation}, "
                    f"error_type={self._safe_error_type(cleanup_error)}).",
                )
        outcome = bool(current and success)
        monitor = self.performance_monitor
        if monitor is not None:
            monitor.record(
                "mcp",
                "runtime_connect",
                (time.monotonic() - started) * 1000,
                status="ok" if outcome else "error",
                attributes={
                    "server_name": config.name,
                    "generation": generation,
                    "tool_count": len(tools) if outcome else 0,
                    "error_type": error_type,
                },
            )
        return outcome

    def disconnect_server(self, server_name: str) -> bool:
        if not self._loop:
            return False

        started = time.monotonic()

        with self._state_lock:
            self._suppressed_servers.add(server_name)
            slot = self._slot_locked(server_name)
            slot.generation += 1
            generation = slot.generation
            client = slot.client or self._clients.get(server_name)
            previous_in_flight = slot.in_flight
            slot.state = MCPRuntimeState.DISCONNECTING
            slot.client = None
            slot.tools = ()
            slot.in_flight = None
            slot.last_error_type = None
            self._remove_server_locked(server_name)
            catalog_update = self._catalog_changed_locked()
        if catalog_update is not None:
            snapshot, _ = catalog_update
            self._publish_catalog_snapshot(snapshot, server_name=server_name)
        if previous_in_flight is not None and not previous_in_flight.done():
            previous_in_flight.cancel()
        if client is None:
            with self._state_lock:
                slot.state = MCPRuntimeState.SUPPRESSED
            self._record_runtime_operation(
                "runtime_disconnect",
                started,
                server_name=server_name,
                generation=generation,
                status="ok",
                tool_count=0,
            )
            return server_name in self._initial_server_order

        future = asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop)
        try:
            future.result(timeout=5.0)
        except Exception as error:
            with self._state_lock:
                current = self._runtime_slots.get(server_name)
                if current is not None and current.generation == generation:
                    current.state = MCPRuntimeState.SUPPRESSED
                    current.last_error_type = self._safe_error_type(error)
            self._emit(
                "error",
                f"MCP disconnect cleanup failed (server={server_name}, "
                f"generation={generation}, "
                f"error_type={self._safe_error_type(error)}).",
            )
            self._record_runtime_operation(
                "runtime_disconnect",
                started,
                server_name=server_name,
                generation=generation,
                status="error",
                error_type=self._safe_error_type(error),
                tool_count=0,
            )
            return False

        with self._state_lock:
            current = self._runtime_slots.get(server_name)
            if current is not None and current.generation == generation:
                current.state = MCPRuntimeState.SUPPRESSED
        self._record_runtime_operation(
            "runtime_disconnect",
            started,
            server_name=server_name,
            generation=generation,
            status="ok",
            tool_count=0,
        )
        return True

    def _record_runtime_operation(
        self,
        name: str,
        started: float,
        *,
        server_name: str,
        generation: int,
        status: str,
        tool_count: int,
        error_type: str | None = None,
    ) -> None:
        monitor = self.performance_monitor
        if monitor is None:
            return
        monitor.record(
            "mcp",
            name,
            (time.monotonic() - started) * 1000,
            status=status,
            attributes={
                "server_name": server_name,
                "generation": generation,
                "tool_count": tool_count,
                "error_type": error_type,
            },
        )

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
                in_flight = tuple(
                    slot.in_flight
                    for slot in self._runtime_slots.values()
                    if slot.in_flight is not None and not slot.in_flight.done()
                )
                clients = tuple(
                    {
                        id(client): client
                        for client in (
                            *self._clients.values(),
                            *(
                                slot.client
                                for slot in self._runtime_slots.values()
                                if slot.client is not None
                            ),
                        )
                    }.values()
                )
            for runtime_future in in_flight:
                runtime_future.cancel()
            if in_flight:
                await asyncio.gather(
                    *(asyncio.wrap_future(item) for item in in_flight),
                    return_exceptions=True,
                )
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
            for slot in self._runtime_slots.values():
                slot.generation += 1
                slot.state = MCPRuntimeState.UNSTARTED
                slot.client = None
                slot.tools = ()
                slot.in_flight = None
                slot.last_error_type = None
        self._initial_future = None

    def get_tool(self, name: str) -> MCPTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None
