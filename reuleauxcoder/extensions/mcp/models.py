"""MCP data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MCPToolInfo:
    """Tool metadata from an MCP server."""

    name: str
    description: str
    input_schema: dict
    server_name: str | None = None


@dataclass(slots=True)
class MCPServerStatus:
    """Structured status snapshot for one configured MCP server."""

    name: str
    enabled: bool
    runtime_connected: bool


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

