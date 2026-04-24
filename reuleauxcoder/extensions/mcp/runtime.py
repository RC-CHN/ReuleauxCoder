"""Shared MCP runtime operations and status helpers."""

from __future__ import annotations

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.extensions.mcp.models import (
    MCPServerStatus,
    MCPServersView,
    MCPToggleResult,
)


def find_mcp_server(
    servers: list[MCPServerConfig], server_name: str
) -> MCPServerConfig | None:
    """Find one configured MCP server by name."""
    for server in servers:
        if server.name == server_name:
            return server
    return None


def refresh_mcp_runtime_tools(agent) -> None:
    """Replace current MCP tools on the agent with manager-provided runtime tools."""
    manager = getattr(agent, "mcp_manager", None)
    manager_tools = list(getattr(manager, "tools", []) or [])
    non_mcp_tools = [
        tool
        for tool in getattr(agent, "tools", [])
        if getattr(tool, "tool_source", None) != "mcp"
    ]
    agent.tools = non_mcp_tools + manager_tools


def build_mcp_servers_view(config, agent=None) -> MCPServersView:
    """Build a structured status snapshot for configured MCP servers."""
    servers = list(getattr(config, "mcp_servers", []) or [])
    manager = getattr(agent, "mcp_manager", None) if agent is not None else None
    runtime_connected = set(getattr(manager, "connected_servers", set()) or set())

    return MCPServersView(
        servers=[
            MCPServerStatus(
                name=server.name,
                enabled=bool(getattr(server, "enabled", True)),
                runtime_connected=server.name in runtime_connected,
            )
            for server in servers
        ]
    )


def toggle_mcp_server(
    server_name: str,
    *,
    enabled: bool,
    agent,
    config,
    store: WorkspaceConfigStore | None = None,
) -> MCPToggleResult:
    """Enable or disable one MCP server and try to apply it at runtime."""
    action = "enable" if enabled else "disable"
    if not server_name:
        return MCPToggleResult(
            server_name="",
            enabled=enabled,
            error=f"Usage: /mcp {action} <server>",
        )

    servers = list(getattr(config, "mcp_servers", []) or [])
    server = find_mcp_server(servers, server_name)
    if server is None:
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            error=f"MCP server '{server_name}' not found in config.",
        )

    if bool(getattr(server, "enabled", True)) == enabled:
        state = "enabled" if enabled else "disabled"
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            already_in_desired_state=True,
            message=f"MCP server '{server_name}' is already {state}.",
        )

    server.enabled = enabled
    config_store = store or WorkspaceConfigStore()
    path = config_store.save_mcp_server_config(server)

    manager = getattr(agent, "mcp_manager", None)
    if manager is None:
        if enabled:
            warning = "MCP manager is not initialized; change is saved and will apply on next startup."
        else:
            warning = "MCP manager is not initialized; disable state is saved."
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            config_saved=True,
            manager_initialized=False,
            saved_path=path,
            message=f"Saved MCP server '{server_name}' to {path}",
            warning=warning,
        )

    ok = (
        manager.connect_server(server)
        if enabled
        else manager.disconnect_server(server_name)
    )
    refresh_mcp_runtime_tools(agent)

    state = "enabled" if enabled else "disabled"
    if ok:
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            config_saved=True,
            runtime_applied=True,
            manager_initialized=True,
            saved_path=path,
            message=f"MCP server '{server_name}' {state} and saved to {path}",
        )

    return MCPToggleResult(
        server_name=server_name,
        enabled=enabled,
        config_saved=True,
        runtime_applied=False,
        manager_initialized=True,
        saved_path=path,
        warning=f"MCP server '{server_name}' state saved to {path}, but runtime {state} failed.",
    )
