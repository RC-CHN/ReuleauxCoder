"""Builtin MCP command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.markdown import Markdown
from rich.panel import Panel

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.extensions.mcp.runtime import build_mcp_servers_view, toggle_mcp_server
from reuleauxcoder.interfaces.cli.views.common import stop_stream_and_clear
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.interfaces.view_registration import register_view


@dataclass(frozen=True, slots=True)
class ToggleMCPServerCommand:
    server_name: str
    enabled: bool


def _parse_show_mcp(user_input: str, parse_ctx):
    if matches_any(user_input, ("/mcp", "/mcp show")):
        return EmptyCommand()
    return None


def _parse_enable_mcp(user_input: str, parse_ctx):
    captures = match_template(user_input, "/mcp enable {server+}")
    if captures is None:
        return None

    try:
        server_name = non_empty_text().parse(captures["server"])
    except ParamParseError:
        return None

    return ToggleMCPServerCommand(server_name=server_name, enabled=True)


def _parse_disable_mcp(user_input: str, parse_ctx):
    captures = match_template(user_input, "/mcp disable {server+}")
    if captures is None:
        return None

    try:
        server_name = non_empty_text().parse(captures["server"])
    except ParamParseError:
        return None

    return ToggleMCPServerCommand(server_name=server_name, enabled=False)


@register_view(view_type="mcp_servers", ui_targets={"cli"})
def render_mcp_servers_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    servers = payload.get("servers") or []
    stop_stream_and_clear(renderer)
    if not servers:
        renderer.console.print(
            Panel("No MCP servers configured.", title="MCP Servers", border_style="blue")
        )
        return True

    lines = []
    for server in servers:
        enabled_mark = "enabled" if server.get("enabled") else "disabled"
        runtime_mark = "connected" if server.get("runtime_connected") else "disconnected"
        lines.append(f"- **{server.get('name', '')}**: {enabled_mark}, runtime={runtime_mark}")
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="MCP Servers", border_style="blue")
    )
    return True


def _is_local_runtime(ctx) -> bool:
    for tool in getattr(ctx.agent, "tools", []) or []:
        backend_id = getattr(tool, "backend_id", None)
        if callable(backend_id):
            backend_id = backend_id()
        if backend_id is not None:
            return backend_id == "local"
    return True


def _handle_show_mcp_servers(command, ctx) -> CommandResult:
    if not _is_local_runtime(ctx):
        ctx.ui_bus.error("MCP commands are only available in local runtime.", kind=UIEventKind.MCP)
        return CommandResult(action="continue", payload={"markdown": "MCP local-only."})

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
    if not _is_local_runtime(ctx):
        ctx.ui_bus.error("MCP commands are only available in local runtime.", kind=UIEventKind.MCP)
        return CommandResult(action="continue", payload={"markdown": "MCP local-only."})

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


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="mcp.show",
                feature_id="mcp",
                description="[global][local-only] Show MCP servers and runtime connection state",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp show"),),
                parser=_parse_show_mcp,
                handler=_handle_show_mcp_servers,
            ),
            ActionSpec(
                action_id="mcp.enable",
                feature_id="mcp",
                description="[global][local-only] Enable an MCP server in workspace config",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp enable <server>"),),
                parser=_parse_enable_mcp,
                handler=_handle_toggle_mcp_server,
            ),
            ActionSpec(
                action_id="mcp.disable",
                feature_id="mcp",
                description="[global][local-only] Disable an MCP server in workspace config",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mcp disable <server>"),),
                parser=_parse_disable_mcp,
                handler=_handle_toggle_mcp_server,
            ),
        ]
    )
