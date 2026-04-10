"""Builtin MCP command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.extensions.command.builtin.common import EmptyCommand, TEXT_REQUIRED, UI_TARGETS, slash_trigger
from reuleauxcoder.extensions.mcp.runtime import build_mcp_servers_view, toggle_mcp_server
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class ToggleMCPServerCommand:
    server_name: str
    enabled: bool


def _parse_show_mcp(user_input: str, parse_ctx):
    if user_input in {"/mcp", "/mcp show"}:
        return EmptyCommand()
    return None


def _parse_enable_mcp(user_input: str, parse_ctx):
    if user_input.startswith("/mcp enable "):
        return ToggleMCPServerCommand(server_name=user_input[len("/mcp enable ") :].strip(), enabled=True)
    return None


def _parse_disable_mcp(user_input: str, parse_ctx):
    if user_input.startswith("/mcp disable "):
        return ToggleMCPServerCommand(server_name=user_input[len("/mcp disable ") :].strip(), enabled=False)
    return None


def _handle_show_mcp_servers(command, ctx) -> CommandResult:
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


def _handle_toggle_mcp_server(command, ctx) -> CommandResult:
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


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="mcp.show",
                feature_id="mcp",
                description="Show MCP servers",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp show"),),
                parser=_parse_show_mcp,
                handler=_handle_show_mcp_servers,
            ),
            ActionSpec(
                action_id="mcp.enable",
                feature_id="mcp",
                description="Enable MCP server",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp enable <server>"),),
                parser=_parse_enable_mcp,
                handler=_handle_toggle_mcp_server,
            ),
            ActionSpec(
                action_id="mcp.disable",
                feature_id="mcp",
                description="Disable MCP server",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp disable <server>"),),
                parser=_parse_disable_mcp,
                handler=_handle_toggle_mcp_server,
            ),
        ]
    )
