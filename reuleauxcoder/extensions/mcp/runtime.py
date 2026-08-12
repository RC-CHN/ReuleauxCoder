"""Shared MCP runtime operations and status helpers."""

from __future__ import annotations

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.extensions.mcp.models import (
    MCPRuntimeStatus,
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
    replace = getattr(agent, "replace_mcp_tools", None)
    if callable(replace):
        replace(manager_tools)
        return
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
    runtime_active = set(getattr(manager, "active_servers", set()) or set())
    initial_state = str(getattr(manager, "initial_state", "idle"))
    runtime_statuses = {
        status.server_name: status
        for status in (getattr(manager, "runtime_statuses", ()) or ())
        if isinstance(status, MCPRuntimeStatus)
    }

    return MCPServersView(
        servers=[
            MCPServerStatus(
                name=server.name,
                enabled=bool(getattr(server, "enabled", True)),
                runtime_connected=server.name in runtime_connected,
                runtime_active=server.name in runtime_active,
                runtime_state=(
                    runtime_statuses[server.name].state.value
                    if server.name in runtime_statuses
                    else "connecting"
                    if bool(getattr(server, "enabled", True))
                    and initial_state == "connecting"
                    and server.name not in runtime_connected
                    else "active"
                    if server.name in runtime_active
                    else "connected"
                    if server.name in runtime_connected
                    else "disabled"
                    if not bool(getattr(server, "enabled", True))
                    else "unavailable"
                ),
                generation=(
                    runtime_statuses[server.name].generation
                    if server.name in runtime_statuses
                    else 0
                ),
                tool_count=(
                    runtime_statuses[server.name].tool_count
                    if server.name in runtime_statuses
                    else 0
                ),
                error_type=(
                    runtime_statuses[server.name].error_type
                    if server.name in runtime_statuses
                    else None
                ),
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

    manager = getattr(agent, "mcp_manager", None)
    configured_enabled = bool(getattr(server, "enabled", True))
    configured_changed = configured_enabled != enabled
    active = set(getattr(manager, "active_servers", set()) or set())
    initial_state = str(getattr(manager, "initial_state", "idle"))

    if not configured_changed and (
        not enabled or server_name in active or initial_state == "connecting"
    ):
        state = "enabled" if enabled else "disabled"
        suffix = (
            " and is connecting"
            if enabled and initial_state == "connecting"
            else ""
        )
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            already_in_desired_state=True,
            message=f"MCP server '{server_name}' is already {state}{suffix}.",
        )

    path = None
    if configured_changed:
        server.enabled = enabled
        config_store = store or WorkspaceConfigStore()
        path = config_store.save_mcp_server_enabled(server.name, enabled)

    if manager is None:
        if enabled:
            warning = "MCP manager is not initialized; change is saved and will apply on next startup."
        else:
            warning = "MCP manager is not initialized; disable state is saved."
        message = (
            f"Saved MCP server '{server_name}' to {path}"
            if path is not None
            else f"MCP server '{server_name}' remains {action}d in workspace config."
        )
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            config_saved=configured_changed,
            manager_initialized=False,
            saved_path=path,
            message=message,
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
        persisted = f" and saved to {path}" if path is not None else ""
        cache_warning = (
            "MCP tool catalog changed; the stable prompt prefix will be rebuilt "
            "before the next model request."
            if initial_state == "sealed"
            else None
        )
        return MCPToggleResult(
            server_name=server_name,
            enabled=enabled,
            config_saved=configured_changed,
            runtime_applied=True,
            manager_initialized=True,
            saved_path=path,
            message=f"MCP server '{server_name}' {state}{persisted}",
            warning=cache_warning,
        )

    return MCPToggleResult(
        server_name=server_name,
        enabled=enabled,
        config_saved=configured_changed,
        runtime_applied=False,
        manager_initialized=True,
        saved_path=path,
        warning=(
            f"MCP server '{server_name}' preference was saved, but runtime "
            f"{state} failed. It will be retried on the next startup."
            if path is not None
            else f"MCP server '{server_name}' runtime {state} retry failed."
        ),
    )
