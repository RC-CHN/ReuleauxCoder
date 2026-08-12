"""MCP client - connects to MCP servers and calls their tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.events import UIEventBus
    from reuleauxcoder.domain.cancellation import CancellationSignal

from reuleauxcoder import __version__
from reuleauxcoder.extensions.mcp.models import (
    MCPRequestHandle,
    MCPRequestState,
    MCPToolCallResult,
    MCPToolInfo,
)
from reuleauxcoder.infrastructure.platform import get_platform_info


class MCPRequestError(RuntimeError):
    """Base failure for one request lifecycle."""


class MCPRequestNotDispatched(MCPRequestError):
    """The request could be proven not to have entered the transport."""


class MCPRequestTimeout(MCPRequestError):
    """An in-flight request did not settle before its client-owned deadline."""

    def __init__(self, message: str, *, request_id: int):
        super().__init__(message)
        self.request_id = request_id


class MCPRequestTransportLost(MCPRequestError):
    """The transport vanished after a request might have been dispatched."""

    def __init__(self, message: str, *, request_id: int | None = None):
        super().__init__(message)
        self.request_id = request_id


class MCPToolRequestCancelled(MCPRequestError):
    """The local caller abandoned an in-flight tools/call request."""

    def __init__(self, request_id: int):
        super().__init__(f"MCP tools/call request {request_id} was cancelled")
        self.request_id = request_id


_TOOL_RESULT_PROTOCOL_CODES = frozenset(
    {
        "result_not_object",
        "is_error_not_boolean",
        "content_not_array",
        "content_item_not_object",
        "text_content_invalid",
        "resource_content_invalid",
        "image_content_invalid",
        "audio_content_invalid",
        "unsupported_content_type",
    }
)


class MCPToolResultProtocolError(MCPRequestError):
    """A settled ``tools/call`` response violated the result schema."""

    def __init__(self, code: str, *, request_id: int):
        super().__init__("MCP tools/call result violated the protocol schema")
        self.code = (
            code if code in _TOOL_RESULT_PROTOCOL_CODES else "invalid_tool_result"
        )
        self.request_id = (
            request_id if type(request_id) is int and request_id >= 0 else 0
        )


class MCPClient:
    """Async client for communicating with an MCP server via stdio."""

    def __init__(
        self,
        config,
        ui_bus: "UIEventBus | None" = None,
        *,
        on_transport_closed: Callable[[MCPClient, str], None] | None = None,
    ):
        self.config = config
        self._ui_bus = ui_bus
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._tools: list[MCPToolInfo] = []
        self._initialized = False
        self._pending_requests: dict[int, MCPRequestHandle] = {}
        self._receive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task[bool] | None = None
        self._on_transport_closed = on_transport_closed

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
            for prefix in get_platform_info().get_bin_paths():
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
            initialize = await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "reuleauxcoder", "version": __version__},
                },
            )
            result = await self._await_request(initialize)

            if not result:
                self._emit("error", f"Failed to initialize server '{self.config.name}'")
                return False

            await self._notify("notifications/initialized", {})

            tools_request = await self._request("tools/list", {})
            tools_result = await self._await_request(tools_request)
            if tools_result and "tools" in tools_result:
                for t in tools_result["tools"]:
                    self._tools.append(
                        MCPToolInfo(
                            name=t["name"],
                            description=t.get("description", ""),
                            input_schema=t.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                            server_name=self.config.name,
                            annotations=dict(t.get("annotations") or {}),
                        )
                    )

            self._initialized = True
            self._emit(
                "success",
                f"Connected to '{self.config.name}' with {len(self._tools)} tools",
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
        """Share one reconnect attempt across concurrent pre-dispatch callers."""
        task = self._reconnect_task
        if task is None or task.done():
            task = asyncio.create_task(self._reconnect_once())
            self._reconnect_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._reconnect_task is task:
                self._reconnect_task = None

    async def _reconnect_once(self) -> bool:
        """Own one disconnect/connect renewal attempt."""
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
            finally:
                self._receive_task = None

        self._fail_pending(
            MCPRequestTransportLost(
                f"MCP server '{self.config.name}' disconnected"
            )
        )

        writer = self._writer
        process = self._process

        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            wait_closed = getattr(writer, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await asyncio.wait_for(
                        cast(Awaitable[None], wait_closed()),
                        timeout=1.0,
                    )
                except Exception:
                    pass

        if process:
            try:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except Exception:
                    pass

        self._process = None
        self._reader = None
        self._writer = None
        self._initialized = False

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        cancellation_signal: "CancellationSignal | None" = None,
        _retry: bool = False,
    ) -> str | MCPToolCallResult:
        if not self._initialized:
            # Try reconnect once if not initialized
            if not _retry:
                if await self.reconnect():
                    return await self.call_tool(
                        name,
                        arguments,
                        cancellation_signal=cancellation_signal,
                        _retry=True,
                    )
            return "Error: MCP client not connected"

        # Check if process is still alive
        if not self.is_connected():
            if not _retry:
                if await self.reconnect():
                    return await self.call_tool(
                        name,
                        arguments,
                        cancellation_signal=cancellation_signal,
                        _retry=True,
                    )
            return "Error: MCP server connection lost"

        try:
            handle = await self._request(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                },
            )
            # From this point onward the request may have executed. Never
            # reconnect/retry it automatically, regardless of timeout or
            # transport failure.
            result = await self._await_request(
                handle,
                cancellation_signal=cancellation_signal,
            )
            return _parse_tool_call_result(result, request_id=handle.request_id)
        except MCPRequestNotDispatched:
            # A retry is safe only while the transport proves no bytes were
            # accepted for this request.
            if not _retry and not (
                cancellation_signal is not None and cancellation_signal.is_set()
            ):
                if await self.reconnect():
                    return await self.call_tool(
                        name,
                        arguments,
                        cancellation_signal=cancellation_signal,
                        _retry=True,
                    )
            raise

    async def _request(self, method: str, params: dict) -> MCPRequestHandle:
        if not self._writer or not self._reader:
            raise MCPRequestNotDispatched("MCP transport is not connected")

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
        handle = MCPRequestHandle(
            request_id=request_id,
            method=method,
            future=future,
        )
        self._pending_requests[request_id] = handle

        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            # A successful write hands bytes to the transport buffer. Even if
            # drain fails, execution can no longer be ruled out.
            handle.state = MCPRequestState.IN_FLIGHT
            await self._writer.drain()
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            self._emit("error", f"Send error: {e}")
            was_dispatched = handle.state is MCPRequestState.IN_FLIGHT
            handle.state = MCPRequestState.SETTLED
            if not future.done():
                future.set_exception(
                    MCPRequestTransportLost(str(e), request_id=request_id)
                    if was_dispatched
                    else MCPRequestNotDispatched(str(e))
                )
                # The caller receives the raised exception below; consume the
                # duplicate future exception to avoid an unhandled warning.
                future.exception()
            if was_dispatched:
                raise MCPRequestTransportLost(
                    str(e), request_id=request_id
                ) from e
            raise MCPRequestNotDispatched(str(e)) from e
        return handle

    async def _await_request(
        self,
        handle: MCPRequestHandle,
        *,
        timeout: float = 30.0,
        cancellation_signal: "CancellationSignal | None" = None,
    ) -> dict | None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            # Response and cancellation are settled on this event loop.
            # A response already queued/delivered wins before a later cancel.
            if handle.future.done():
                return handle.future.result()
            if cancellation_signal is not None and cancellation_signal.is_set():
                if handle.future.done():
                    return handle.future.result()
                await self._cancel_request(
                    handle,
                    reason="User interrupted the active tool call",
                )
                raise MCPToolRequestCancelled(handle.request_id)
            if asyncio.get_running_loop().time() >= deadline:
                current = self._pending_requests.get(handle.request_id)
                if current is handle:
                    self._pending_requests.pop(handle.request_id, None)
                handle.state = MCPRequestState.SETTLED
                if not handle.future.done():
                    handle.future.cancel()
                self._emit("warning", f"Request timeout: {handle.method}")
                raise MCPRequestTimeout(
                    f"{handle.method} request {handle.request_id} timed out",
                    request_id=handle.request_id,
                )
            await asyncio.sleep(0.05)

    async def _cancel_request(
        self, handle: MCPRequestHandle, *, reason: str
    ) -> bool:
        if handle.state is MCPRequestState.SETTLED or handle.future.done():
            return False
        current = self._pending_requests.get(handle.request_id)
        if current is not handle:
            return False
        # Remove first so a late response cannot win after cancellation has
        # become the terminal local result.
        self._pending_requests.pop(handle.request_id, None)
        handle.state = MCPRequestState.SETTLED
        if not handle.future.done():
            handle.future.cancel()
        if not handle.cancellation_sent:
            handle.cancellation_sent = True
            self._notify_detached(
                "notifications/cancelled",
                {"requestId": handle.request_id, "reason": reason},
            )
        return True

    def _notify_detached(self, method: str, params: dict) -> None:
        """Queue a fire-and-forget notification without delaying cancellation."""
        writer = self._writer
        if writer is None:
            return
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            writer.write((json.dumps(message) + "\n").encode())
        except Exception as error:
            self._emit("error", f"Notify error: {error}")
            return

        async def finish_drain() -> None:
            try:
                await writer.drain()
            except Exception as error:
                self._emit("error", f"Notify error: {error}")

        asyncio.create_task(finish_drain())

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
        unexpected_error_type: str | None = None
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
                        handle = self._pending_requests.pop(message["id"])
                        handle.state = MCPRequestState.SETTLED
                        future = handle.future
                        if not future.done():
                            if "error" in message:
                                future.set_result(None)
                            else:
                                future.set_result(message.get("result"))
                    elif "id" in message:
                        self._emit(
                            "warning",
                            f"Ignored late result for request {message['id']}",
                        )

                    if message.get("method") == "notifications/message":
                        params = message.get("params", {})
                        level = params.get("level", "info")
                        data = params.get("data", "")
                        if level in ("error", "warning"):
                            self._emit(level, f"[{self.config.name}] {data}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            unexpected_error_type = type(e).__name__
            self._emit("error", f"Receive error: {e}")
            self._fail_pending(MCPRequestTransportLost(str(e)))
        else:
            unexpected_error_type = "TransportEOF"
            self._fail_pending(
                MCPRequestTransportLost(
                    f"MCP server '{self.config.name}' closed its output stream"
                )
            )
        finally:
            self._initialized = False
            callback = self._on_transport_closed
            if callback is not None and unexpected_error_type is not None:
                try:
                    callback(self, unexpected_error_type)
                except Exception as error:
                    self._emit(
                        "error",
                        "Transport-state observer failed "
                        f"(error_type={type(error).__name__})",
                    )

    def _fail_pending(self, error: MCPRequestError) -> None:
        pending = tuple(self._pending_requests.values())
        self._pending_requests.clear()
        for handle in pending:
            handle.state = MCPRequestState.SETTLED
            if not handle.future.done():
                pending_error = error
                if isinstance(error, MCPRequestTransportLost):
                    pending_error = MCPRequestTransportLost(
                        str(error),
                        request_id=handle.request_id,
                    )
                handle.future.set_exception(pending_error)


def _parse_tool_call_result(
    result: object,
    *,
    request_id: int,
) -> MCPToolCallResult:
    """Validate and project one MCP ``CallToolResult`` without guessing shapes."""
    if not isinstance(result, dict):
        raise MCPToolResultProtocolError("result_not_object", request_id=request_id)

    raw_is_error = result.get("isError", False)
    if type(raw_is_error) is not bool:
        raise MCPToolResultProtocolError("is_error_not_boolean", request_id=request_id)

    if "content" not in result or not isinstance(result["content"], list):
        raise MCPToolResultProtocolError("content_not_array", request_id=request_id)
    content = result["content"]

    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            raise MCPToolResultProtocolError(
                "content_item_not_object", request_id=request_id
            )
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise MCPToolResultProtocolError(
                    "text_content_invalid", request_id=request_id
                )
            text_parts.append(text)
        elif item_type == "resource":
            resource = item.get("resource")
            if not isinstance(resource, dict) or not isinstance(
                resource.get("uri"), str
            ):
                raise MCPToolResultProtocolError(
                    "resource_content_invalid", request_id=request_id
                )
            text_parts.append(f"[Resource: {resource['uri']}]")
        elif item_type in {"image", "audio"}:
            mime_type = item.get("mimeType")
            data = item.get("data")
            if not isinstance(mime_type, str) or not isinstance(data, str):
                raise MCPToolResultProtocolError(
                    f"{item_type}_content_invalid", request_id=request_id
                )
            label = "Image" if item_type == "image" else "Audio"
            text_parts.append(f"[{label}: {mime_type}, {len(data)} chars base64]")
        else:
            raise MCPToolResultProtocolError(
                "unsupported_content_type", request_id=request_id
            )

    if raw_is_error:
        # CallToolResult content is the protocol-defined business-error channel
        # intended for the model. Host exceptions never enter this value.
        return MCPToolCallResult(
            content="\n".join(text_parts) or "(no error details)",
            is_error=True,
            request_id=request_id,
            error_content_items=len(content),
        )
    return MCPToolCallResult(
        content="\n".join(text_parts) or "(no output)",
        is_error=False,
        request_id=request_id,
    )
