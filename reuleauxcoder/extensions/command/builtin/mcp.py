"""Builtin MCP command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.extensions.mcp.runtime import (
    build_mcp_servers_view,
    toggle_mcp_server,
)
from reuleauxcoder.interfaces.events import UIEventKind


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


def _is_local_runtime(ctx) -> bool:
    for tool in getattr(ctx.agent, "tools", []) or []:
        backend_id = getattr(tool, "backend_id", None)
        if callable(backend_id):
            backend_id = backend_id()
        if backend_id is not None:
            return backend_id == "local"
    return True


def _handle_show_mcp_servers(command, ctx) -> CommandEffect:
    if not _is_local_runtime(ctx):
        ctx.effect.error(
            "MCP commands are only available in local runtime.", kind=UIEventKind.MCP
        )
        return ctx.effect.finish(control="continue")

    view = build_mcp_servers_view(ctx.config, ctx.agent)
    payload = view.to_payload()
    ctx.effect.open_view(
        view.view_type,
        title="MCP Servers",
        view_model=view,
        reuse_key="mcp_servers",
    )
    return ctx.effect.finish(control="continue", state_changes=payload)


def _handle_toggle_mcp_server(command, ctx) -> CommandEffect:
    if not _is_local_runtime(ctx):
        ctx.effect.error(
            "MCP commands are only available in local runtime.", kind=UIEventKind.MCP
        )
        return ctx.effect.finish(control="continue")

    result = toggle_mcp_server(
        command.server_name,
        enabled=command.enabled,
        agent=ctx.agent,
        config=ctx.config,
    )

    if result.error:
        ctx.effect.error(
            result.error, kind=UIEventKind.MCP, server_name=result.server_name
        )
        return ctx.effect.finish(control="continue")

    if result.message and result.already_in_desired_state:
        ctx.effect.info(
            result.message, kind=UIEventKind.MCP, server_name=result.server_name
        )
        return ctx.effect.finish(control="continue")

    if result.warning:
        ctx.effect.warning(
            result.warning, kind=UIEventKind.MCP, server_name=result.server_name
        )
    if result.message:
        ctx.effect.success(
            result.message,
            kind=UIEventKind.MCP,
            server_name=result.server_name,
            enabled=result.enabled,
            saved_path=str(result.saved_path) if result.saved_path else None,
        )

    view = build_mcp_servers_view(ctx.config, ctx.agent)
    ctx.effect.refresh_view(
        view.view_type,
        title="MCP Servers",
        view_model=view,
        reuse_key="mcp_servers",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
