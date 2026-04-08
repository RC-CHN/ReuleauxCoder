"""MCP manager - manages MCP servers and tool aggregation."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.events import UIEventBus

from reuleauxcoder.extensions.mcp.adapter import MCPTool
from reuleauxcoder.extensions.mcp.client import MCPClient


class MCPManager:
    """Manages connections to multiple MCP servers and aggregates their tools."""

    def __init__(self, ui_bus: "UIEventBus | None" = None):
        self._ui_bus = ui_bus
        self._clients: list[MCPClient] = []
        self._tools: list[MCPTool] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self):
        if self._started:
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started = True

        while not self._loop.is_running():
            time.sleep(0.01)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self):
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
        return self._tools

    def connect_server(self, config) -> bool:
        if not self._started:
            self.start()

        client = MCPClient(config, ui_bus=self._ui_bus)
        future = asyncio.run_coroutine_threadsafe(client.connect(), self._loop)
        try:
            success = future.result(timeout=30.0)
        except Exception as e:
            if self._ui_bus:
                from reuleauxcoder.interfaces.events import UIEventKind

                self._ui_bus.error(f"Connection error: {e}", kind=UIEventKind.MCP)
            return False

        if success:
            self._clients.append(client)
            for tool_info in client.tools:
                self._tools.append(MCPTool(client, tool_info, self._loop))

        return success

    def disconnect_all(self):
        if not self._loop:
            return

        async def _disconnect():
            for client in self._clients:
                await client.disconnect()

        future = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
        try:
            future.result(timeout=5.0)
        except Exception:
            pass

        self._clients.clear()
        self._tools.clear()

    def get_tool(self, name: str) -> MCPTool | None:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None
