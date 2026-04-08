"""MCP adapter - wraps MCP tools as internal tools."""

import asyncio
import concurrent.futures

from reuleauxcoder.extensions.mcp.client import MCPClient
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.tools.base import Tool


class MCPTool(Tool):
    """Wraps an MCP tool as an internal Tool instance."""

    tool_source = "mcp"

    def __init__(
        self, client: MCPClient, tool_info: MCPToolInfo, loop: asyncio.AbstractEventLoop
    ):
        self._client = client
        self._tool_info = tool_info
        self._loop = loop
        self.name = tool_info.name
        self.description = tool_info.description
        self.parameters = tool_info.input_schema
        self.server_name = tool_info.server_name

    def execute(self, **kwargs) -> str:
        if self._loop is None or not self._loop.is_running():
            return "Error: MCP event loop not running"

        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(self._tool_info.name, kwargs),
            self._loop,
        )
        try:
            return future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            return "Error: MCP tool call timed out"
        except Exception as e:
            return f"Error: {e}"
