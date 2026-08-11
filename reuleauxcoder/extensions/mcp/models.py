"""MCP data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass
class MCPToolInfo:
    """Tool metadata from an MCP server."""

    name: str
    description: str
    input_schema: dict
    server_name: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


class MCPRequestState(str, Enum):
    NOT_DISPATCHED = "not_dispatched"
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"


@dataclass(slots=True)
class MCPRequestHandle:
    """One request identity whose result/cancel race is owned by the MCP loop."""

    request_id: int
    method: str
    future: Any
    state: MCPRequestState = MCPRequestState.NOT_DISPATCHED
    cancellation_sent: bool = False


@dataclass(frozen=True, slots=True)
class MCPToolCallResult:
    """Protocol facts returned by one settled ``tools/call`` request."""

    content: str
    is_error: bool
    request_id: int
    error_content_items: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if type(self.is_error) is not bool:
            raise TypeError("is_error must be a boolean")
        if type(self.request_id) is not int or self.request_id < 0:
            raise ValueError("request_id must be a non-negative integer")
        if type(self.error_content_items) is not int or self.error_content_items < 0:
            raise ValueError("error_content_items must be a non-negative integer")


@dataclass(slots=True)
class MCPServerStatus:
    """Structured status snapshot for one configured MCP server."""

    name: str
    enabled: bool
    runtime_connected: bool
    runtime_active: bool = False
    runtime_state: str = "unavailable"


@dataclass(slots=True)
class MCPToggleResult:
    """Structured result for enabling/disabling an MCP server."""

    server_name: str
    enabled: bool
    config_saved: bool = False
    runtime_applied: bool = False
    already_in_desired_state: bool = False
    manager_initialized: bool = False
    saved_path: Path | None = None
    message: str | None = None
    warning: str | None = None
    error: str | None = None


@dataclass(slots=True)
class MCPServersView:
    """Structured view payload for MCP server listings."""

    servers: list[MCPServerStatus] = field(default_factory=list)
    view_type: str = "mcp_servers"

    def to_payload(self) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "name": server.name,
                    "enabled": server.enabled,
                    "runtime_connected": server.runtime_connected,
                }
                for server in self.servers
            ]
        }
