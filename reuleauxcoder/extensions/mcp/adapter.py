"""MCP adapter - wraps MCP tools as internal tools."""

import asyncio

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.mcp.client import (
    MCPClient,
    MCPRequestNotDispatched,
    MCPRequestTimeout,
    MCPRequestTransportLost,
    MCPToolResultProtocolError,
    MCPToolRequestCancelled,
)
from reuleauxcoder.extensions.mcp.models import MCPToolCallResult, MCPToolInfo
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
            return _mcp_adapter_failure(
                phase="availability",
                error_type="MCPEventLoopUnavailable",
                effect_state="not_started",
            )

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
            result = future.result()
            if isinstance(result, MCPToolCallResult):
                if result.is_error:
                    return _reported_mcp_failure(result)
                return result.content
            if isinstance(result, str):
                return ToolOutcome.from_legacy(result)
            return _mcp_adapter_failure(
                phase="adapter_result",
                error_type="MCPAdapterResultType",
                effect_state="unknown",
            )
        except MCPToolRequestCancelled as error:
            message = (
                "MCP tool call was cancelled "
                "(phase=request_wait, error_type=MCPToolRequestCancelled, "
                f"request_id={error.request_id}, effect_state=unknown). "
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
                    "failure_phase": "request_wait",
                    "error_type": "MCPToolRequestCancelled",
                    "effect_state": "unknown",
                    "mcp_request_id": error.request_id,
                },
            )
        except (MCPRequestTimeout, MCPRequestTransportLost) as error:
            request_id = error.request_id
            timed_out = isinstance(error, MCPRequestTimeout)
            phase = "request_wait" if timed_out else "transport"
            error_type = type(error).__name__
            message = (
                "MCP tool call lost its authoritative result "
                f"(phase={phase}, error_type={error_type}"
                f"{f', request_id={request_id}' if request_id is not None else ''}, "
                "effect_state=unknown). The request was already in flight, so the operation may "
                "have completed. Inspect server state before deciding whether to "
                "retry, and do not blindly repeat it."
            )
            metadata = {
                "failure_phase": phase,
                "error_type": error_type,
                "effect_state": "unknown",
            }
            if request_id is not None:
                metadata["mcp_request_id"] = request_id
            return ToolOutcome(
                status=(
                    ToolOutcomeStatus.TIMED_OUT
                    if timed_out
                    else ToolOutcomeStatus.FAILED
                ),
                summary=f"{self.name} result unknown",
                content=message,
                model_content=message,
                error_kind=ToolErrorKind.EXECUTION,
                metadata=metadata,
            )
        except MCPToolResultProtocolError as error:
            return _mcp_protocol_failure(error)
        except MCPRequestNotDispatched:
            return _mcp_adapter_failure(
                phase="dispatch",
                error_type="MCPRequestNotDispatched",
                effect_state="not_started",
            )
        except Exception as error:
            return _mcp_adapter_failure(
                phase="adapter",
                error_type=_safe_error_type(error),
                effect_state="unknown",
            )


def _reported_mcp_failure(result: MCPToolCallResult) -> ToolOutcome:
    """Project the protocol-defined MCP business failure as a failed outcome."""
    facts = (
        "MCP tool failed "
        "(phase=tool_result, error_type=MCPToolReportedError, "
        f"request_id={result.request_id}, effect_state=server_reported_failure, "
        "details=server_error_content). "
        "Inspect server state before retrying if the tool may mutate state."
    )
    message = f"{facts}\n\n{result.content}"
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        summary="MCP tool reported failure",
        content=message,
        model_content=message,
        error_kind=ToolErrorKind.EXECUTION,
        metadata={
            "failure_phase": "tool_result",
            "error_type": "MCPToolReportedError",
            "mcp_request_id": result.request_id,
            "effect_state": "server_reported_failure",
            "error_detail_state": "server_error_content",
            "error_content_items": result.error_content_items,
        },
    )


def _mcp_protocol_failure(error: MCPToolResultProtocolError) -> ToolOutcome:
    message = (
        "MCP tool failed "
        "(phase=tool_result_protocol, error_type=MCPToolResultProtocolError, "
        f"protocol_error_code={error.code}, request_id={error.request_id}, "
        "effect_state=unknown). Inspect server state before retrying."
    )
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        summary="MCP tool returned an invalid result",
        content=message,
        model_content=message,
        error_kind=ToolErrorKind.EXECUTION,
        metadata={
            "failure_phase": "tool_result_protocol",
            "error_type": "MCPToolResultProtocolError",
            "protocol_error_code": error.code,
            "mcp_request_id": error.request_id,
            "effect_state": "unknown",
        },
    )


def _mcp_adapter_failure(
    *,
    phase: str,
    error_type: str,
    effect_state: str,
) -> ToolOutcome:
    message = (
        "MCP tool failed "
        f"(phase={phase}, error_type={error_type}, effect_state={effect_state})."
    )
    if effect_state == "unknown":
        message += " Inspect server state before retrying."
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        summary="MCP tool failed",
        content=message,
        model_content=message,
        error_kind=ToolErrorKind.EXECUTION,
        metadata={
            "failure_phase": phase,
            "error_type": error_type,
            "effect_state": effect_state,
        },
    )


def _safe_error_type(error: BaseException) -> str:
    raw = type(error).__name__
    safe = "".join(
        character
        for character in raw
        if character.isascii() and (character.isalnum() or character in {"_", "-", "."})
    )[:64]
    return safe or "Error"
