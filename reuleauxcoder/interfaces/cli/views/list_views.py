"""CLI renderers for list-oriented structured views."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel

from reuleauxcoder.interfaces.events import UIEvent
from reuleauxcoder.interfaces.cli.views.common import stop_stream_and_clear


def render_sessions_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    sessions = payload.get("sessions") or []
    stop_stream_and_clear(renderer)
    if not sessions:
        renderer.console.print(Panel("No saved sessions.", title="Saved Sessions", border_style="blue"))
        return True

    lines = []
    for session in sessions:
        lines.append(
            f"- `{session.get('id', '')}` ({session.get('model', '')}, {session.get('saved_at', '')}) {session.get('preview', '')}"
        )
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="Saved Sessions", border_style="blue")
    )
    return True


def render_mcp_servers_view(renderer, event: UIEvent) -> bool:
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
