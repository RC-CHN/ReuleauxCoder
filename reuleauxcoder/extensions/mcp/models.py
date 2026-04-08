"""MCP data models."""

from dataclasses import dataclass, field


@dataclass
class MCPToolInfo:
    """Tool metadata from an MCP server."""

    name: str
    description: str
    input_schema: dict
    server_name: str | None = None
