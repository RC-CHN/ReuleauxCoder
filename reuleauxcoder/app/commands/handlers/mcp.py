"""Shared handlers for MCP-related commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    CommandContext,
    CommandResult,
    OpenViewRequest,
    ShowMCPServersCommand,
    ToggleMCPServerCommand,
)
from reuleauxcoder.extensions.mcp.runtime import build_mcp_servers_view, toggle_mcp_server
from reuleauxcoder.interfaces.events import UIEventKind


def handle_show_mcp_servers(command: ShowMCPServersCommand, ctx: CommandContext) -> CommandResult:
    """Show configured MCP servers and runtime connectivity state."""
    view = build_mcp_servers_view(ctx.config, ctx.agent)
    payload = view.to_payload()
    if not view.servers:
        ctx.ui_bus.info("No MCP servers configured.", kind=UIEventKind.MCP)
        return CommandResult(action="continue", payload=payload)

    ctx.ui_bus.open_view(
        "mcp_servers",
        title="MCP Servers",
        payload=payload,
        reuse_key="mcp_servers",
    )
    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="mcp_servers",
                title="MCP Servers",
                payload=payload,
                reuse_key="mcp_servers",
            )
        ],
        payload=payload,
    )


def handle_toggle_mcp_server(command: ToggleMCPServerCommand, ctx: CommandContext) -> CommandResult:
    """Enable or disable one MCP server and refresh the MCP servers view."""
    result = toggle_mcp_server(
        command.server_name,
        enabled=command.enabled,
        agent=ctx.agent,
        config=ctx.config,
    )

    if result.error:
        ctx.ui_bus.error(result.error, kind=UIEventKind.MCP, server_name=result.server_name)
        return CommandResult(action="continue")

    if result.message and result.already_in_desired_state:
        ctx.ui_bus.info(result.message, kind=UIEventKind.MCP, server_name=result.server_name)
        return CommandResult(action="continue")

    if result.warning:
        ctx.ui_bus.warning(result.warning, kind=UIEventKind.MCP, server_name=result.server_name)
    if result.message:
        ctx.ui_bus.success(
            result.message,
            kind=UIEventKind.MCP,
            server_name=result.server_name,
            enabled=result.enabled,
            saved_path=str(result.saved_path) if result.saved_path else None,
        )

    view = build_mcp_servers_view(ctx.config, ctx.agent)
    ctx.ui_bus.refresh_view(
        "mcp_servers",
        title="MCP Servers",
        payload=view.to_payload(),
        reuse_key="mcp_servers",
    )
    return CommandResult(action="continue", payload=view.to_payload())
