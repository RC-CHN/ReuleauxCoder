"""MCP adapter - wraps MCP tools as internal tools."""

import asyncio

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.mcp.client import (
    MCPClient,
    MCPRequestTimeout,
    MCPRequestTransportLost,
    MCPToolRequestCancelled,
)
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.tools.base import InterruptMode, Tool


class MCPTool(Tool):
    """Wraps an MCP tool as an internal Tool instance."""

    tool_source = "mcp"
    interrupt_mode = InterruptMode.CANCEL_WITH_PARTIAL

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
        self.annotations = dict(tool_info.annotations)

    def execute(self, **kwargs) -> str | ToolOutcome:
        if self._loop is None or not self._loop.is_running():
            return "Error: MCP event loop not running"

        cancellation = self.current_cancellation_signal()
        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(
                self._tool_info.name,
                kwargs,
                cancellation_signal=cancellation,
            ),
            self._loop,
        )
        try:
            return future.result()
        except MCPToolRequestCancelled as error:
            message = (
                f"MCP tool call was cancelled locally (request {error.request_id}). "
                "The server may ignore cancellation or may already have completed "
                "the operation. Its side effects are unknown; inspect server state "
                "before deciding whether to retry, and do not blindly repeat it."
            )
            return ToolOutcome(
                status=ToolOutcomeStatus.CANCELLED,
                summary=f"{self.name} interrupted",
                content=message,
                model_content=message,
                error_kind=ToolErrorKind.INTERRUPTED,
                metadata={
                    "effect_state": "unknown",
                    "mcp_request_id": error.request_id,
                    "mcp_server": self.server_name,
                },
            )
        except (MCPRequestTimeout, MCPRequestTransportLost) as error:
            request_id = error.request_id
            message = (
                f"MCP tool call lost its authoritative result"
                f"{f' (request {request_id})' if request_id is not None else ''}: "
                f"{error}. The request was already in flight, so the operation may "
                "have completed. Inspect server state before deciding whether to "
                "retry, and do not blindly repeat it."
            )
            metadata = {
                "effect_state": "unknown",
                "mcp_server": self.server_name,
            }
            if request_id is not None:
                metadata["mcp_request_id"] = request_id
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                summary=f"{self.name} result unknown",
                content=message,
                model_content=message,
                error_kind=ToolErrorKind.EXECUTION,
                metadata=metadata,
            )
        except Exception as e:
            return f"Error: {e}"
