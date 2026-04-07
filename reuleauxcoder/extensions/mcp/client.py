"""MCP client - connects to MCP servers and calls their tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.events import UIEventBus

from reuleauxcoder.extensions.mcp.models import MCPToolInfo


class MCPClient:
    """Async client for communicating with an MCP server via stdio."""

    def __init__(self, config, ui_bus: "UIEventBus | None" = None):
        self.config = config
        self._ui_bus = ui_bus
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._tools: list[MCPToolInfo] = []
        self._initialized = False
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._receive_task: asyncio.Task | None = None

    @property
    def tools(self) -> list[MCPToolInfo]:
        return self._tools

    def _emit(self, level: str, message: str) -> None:
        """Emit a UI event if bus is available."""
        if not self._ui_bus:
            return
        from reuleauxcoder.interfaces.events import UIEventKind

        method = getattr(self._ui_bus, level, None)
        if method:
            method(f"[MCP] {message}", kind=UIEventKind.MCP)

    async def connect(self) -> bool:
        cmd = shutil.which(self.config.command)
        if not cmd:
            for prefix in [
                "/usr/local/bin",
                "/usr/bin",
                os.path.expanduser("~/.local/bin"),
            ]:
                candidate = os.path.join(prefix, self.config.command)
                if os.path.exists(candidate):
                    cmd = candidate
                    break

        if not cmd:
            self._emit("error", f"Cannot find command: {self.config.command}")
            return False

        env = os.environ.copy()
        env.update(self.config.env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                cmd,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.config.cwd,
            )
            self._reader = self._process.stdout
            self._writer = self._process.stdin
        except Exception as e:
            self._emit("error", f"Failed to start server '{self.config.name}': {e}")
            return False

        self._receive_task = asyncio.create_task(self._receive_loop())

        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "reuleauxcoder", "version": "0.1.0"},
                },
            )

            if not result:
                self._emit("error", f"Failed to initialize server '{self.config.name}'")
                return False

            await self._notify("notifications/initialized", {})

            tools_result = await self._request("tools/list", {})
            if tools_result and "tools" in tools_result:
                for t in tools_result["tools"]:
                    self._tools.append(
                        MCPToolInfo(
                            name=t["name"],
                            description=t.get("description", ""),
                            input_schema=t.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                        )
                    )

            self._initialized = True
            self._emit(
                "success", f"Connected to '{self.config.name}' with {len(self._tools)} tools"
            )
            return True
        except Exception as e:
            self._emit("error", f"Initialization error: {e}")
            return False

    def is_connected(self) -> bool:
        """Check if the MCP server is still connected."""
        if not self._initialized:
            return False
        if not self._process or self._process.returncode is not None:
            return False
        if not self._writer or not self._reader:
            return False
        return True

    async def reconnect(self) -> bool:
        """Disconnect and reconnect to the MCP server."""
        self._emit("warning", f"Attempting to reconnect to '{self.config.name}'...")
        await self.disconnect()
        # Clear tools since they'll be re-fetched on connect
        self._tools = []
        success = await self.connect()
        if success:
            self._emit("success", f"Reconnected to '{self.config.name}'")
        else:
            self._emit("error", f"Failed to reconnect to '{self.config.name}'")
        return success

    async def disconnect(self):
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

        self._process = None
        self._reader = None
        self._writer = None
        self._initialized = False

    async def call_tool(self, name: str, arguments: dict, _retry: bool = False) -> str:
        if not self._initialized:
            # Try reconnect once if not initialized
            if not _retry:
                if await self.reconnect():
                    return await self.call_tool(name, arguments, _retry=True)
            return "Error: MCP client not connected"

        # Check if process is still alive
        if not self.is_connected():
            if not _retry:
                if await self.reconnect():
                    return await self.call_tool(name, arguments, _retry=True)
            return "Error: MCP server connection lost"

        try:
            result = await self._request(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                },
            )

            if not result:
                # No response - might be connection issue, try reconnect
                if not _retry:
                    if await self.reconnect():
                        return await self.call_tool(name, arguments, _retry=True)
                return "Error: No response from MCP server"

            content = result.get("content", [])
            if not content:
                return "(no output)"

            text_parts = []
            for item in content:
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "resource":
                    resource = item.get("resource", {})
                    text_parts.append(f"[Resource: {resource.get('uri', 'unknown')}]")
                elif item.get("type") == "image":
                    mime_type = item.get("mimeType", "unknown")
                    data = item.get("data", "")
                    text_parts.append(f"[Image: {mime_type}, {len(data)} chars base64]")
                elif item.get("type") == "audio":
                    mime_type = item.get("mimeType", "unknown")
                    data = item.get("data", "")
                    text_parts.append(f"[Audio: {mime_type}, {len(data)} chars base64]")

            result_text = "\n".join(text_parts)
            if result.get("isError"):
                return f"Error: {result_text}"
            return result_text or "(no output)"
        except Exception as e:
            # Exception during call - try reconnect once
            if not _retry:
                if await self.reconnect():
                    return await self.call_tool(name, arguments, _retry=True)
            return f"Error calling MCP tool: {e}"

    async def _request(self, method: str, params: dict) -> dict | None:
        if not self._writer or not self._reader:
            return None

        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_requests[request_id] = future

        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            await self._writer.drain()
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            self._emit("error", f"Send error: {e}")
            return None

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            self._emit("warning", f"Request timeout: {method}")
            return None

    async def _notify(self, method: str, params: dict):
        if not self._writer:
            return

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            await self._writer.drain()
        except Exception as e:
            self._emit("error", f"Notify error: {e}")

    async def _receive_loop(self):
        if not self._reader:
            return

        buffer = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue

                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue

                    if "id" in message and message["id"] in self._pending_requests:
                        future = self._pending_requests.pop(message["id"])
                        if not future.done():
                            if "error" in message:
                                future.set_result(None)
                            else:
                                future.set_result(message.get("result"))

                    if message.get("method") == "notifications/message":
                        params = message.get("params", {})
                        level = params.get("level", "info")
                        data = params.get("data", "")
                        if level in ("error", "warning"):
                            self._emit(level, f"[{self.config.name}] {data}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._emit("error", f"Receive error: {e}")
