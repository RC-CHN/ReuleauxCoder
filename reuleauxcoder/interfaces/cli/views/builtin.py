"""CLI renderers for builtin structured command views."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from reuleauxcoder.interfaces.cli.views.common import (
    render_markdown_panel,
    stop_stream_and_clear,
)
from reuleauxcoder.interfaces.view_registry import ViewRendererSpec
from reuleauxcoder.app.commands.view_models import (
    MCPServersViewModel,
    MarkdownViewModel,
    SessionsViewModel,
    EffectiveConfigViewModel,
)


def _markdown_view(title: str):
    def render(renderer, event) -> bool:
        model = event.data.get("view_model")
        return isinstance(model, MarkdownViewModel) and render_markdown_panel(
            renderer, markdown_text=model.markdown, title=title
        )

    return render


def render_mcp_servers_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, MCPServersViewModel):
        return False
    servers = model.servers
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
        enabled = "enabled" if server.enabled else "disabled"
        connected = (
            "connected" if server.runtime_connected else "disconnected"
        )
        lines.append(
            f"- **{server.name}**: {enabled}, runtime={connected}"
        )
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="MCP Servers", border_style="blue")
    )
    return True


def render_sessions_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, SessionsViewModel):
        return False
    sessions = model.sessions
    fingerprint = model.fingerprint
    show_all = model.show_all
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
        suffix = f" [{session.fingerprint or ''}]" if show_all else ""
        lines.append(
            f"- `{session.session_id}` "
            f"({session.model}, {session.saved_at})"
            f"{suffix} {session.preview}"
        )
    renderer.console.print(
        Panel(Markdown("\n".join(lines)), title="Saved Sessions", border_style="blue")
    )
    return True


def render_effective_config_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, EffectiveConfigViewModel):
        return False
    stop_stream_and_clear(renderer)
    table = Table(title="Effective Configuration", show_header=True)
    table.add_column("Path")
    table.add_column("Value")
    table.add_column("Source")
    for row in model.rows:
        table.add_row(row.path, row.value, row.source)
    renderer.console.print(table)
    for diagnostic in model.diagnostics:
        renderer.console.print(f"[yellow]⚠ {diagnostic}[/yellow]")
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
            ViewRendererSpec(
                view_type="effective_config", render=render_effective_config_view
            ),
        ]
    )
    return specs
