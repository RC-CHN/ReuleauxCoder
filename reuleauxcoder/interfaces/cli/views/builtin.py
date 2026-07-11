"""CLI renderers for builtin structured command views."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel

from reuleauxcoder.interfaces.cli.views.common import (
    render_markdown_panel,
    stop_stream_and_clear,
)
from reuleauxcoder.interfaces.view_registry import ViewRendererSpec


def _markdown_view(title: str):
    def render(renderer, event) -> bool:
        payload = event.data.get("payload") or {}
        markdown = payload.get("markdown")
        return isinstance(markdown, str) and render_markdown_panel(
            renderer, markdown_text=markdown, title=title
        )

    return render


def render_mcp_servers_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    servers = payload.get("servers") or []
    stop_stream_and_clear(renderer)
    if not servers:
        renderer.console.print(
            Panel(
                "No MCP servers configured.", title="MCP Servers", border_style="blue"
            )
        )
        return True

    lines = []
    for server in servers:
        enabled = "enabled" if server.get("enabled") else "disabled"
        connected = (
            "connected" if server.get("runtime_connected") else "disconnected"
        )
        lines.append(
            f"- **{server.get('name', '')}**: {enabled}, runtime={connected}"
        )
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="MCP Servers", border_style="blue")
    )
    return True


def render_sessions_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    sessions = payload.get("sessions") or []
    fingerprint = payload.get("fingerprint")
    show_all = bool(payload.get("show_all"))
    scope = "all fingerprints" if show_all else f"fingerprint: {fingerprint or 'local'}"
    stop_stream_and_clear(renderer)
    if not sessions:
        renderer.console.print(
            Panel(
                f"No saved sessions for {scope}",
                title="Saved Sessions",
                border_style="blue",
            )
        )
        return True

    lines = [f"Scope: `{scope}`", ""]
    for session in sessions:
        suffix = f" [{session.get('fingerprint', '')}]" if show_all else ""
        lines.append(
            f"- `{session.get('id', '')}` "
            f"({session.get('model', '')}, {session.get('saved_at', '')})"
            f"{suffix} {session.get('preview', '')}"
        )
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="Saved Sessions", border_style="blue")
    )
    return True


def builtin_cli_view_specs() -> list[ViewRendererSpec]:
    """Return explicit CLI-owned view registrations."""
    markdown_views = {
        "approval_rules": "Approval Rules",
        "help": "Help",
        "mode_profiles": "Mode Profiles",
        "model_profiles": "Model Profiles",
        "skills": "Skills",
        "token_usage": "Token Usage",
    }
    specs = [
        ViewRendererSpec(view_type=name, render=_markdown_view(title))
        for name, title in markdown_views.items()
    ]
    specs.extend(
        [
            ViewRendererSpec(
                view_type="mcp_servers", render=render_mcp_servers_view
            ),
            ViewRendererSpec(view_type="sessions", render=render_sessions_view),
        ]
    )
    return specs
