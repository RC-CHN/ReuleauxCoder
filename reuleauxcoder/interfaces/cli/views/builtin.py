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
    HelpViewModel,
    MCPServersViewModel,
    MarkdownViewModel,
    ModelListViewModel,
    ModesViewModel,
    SessionsViewModel,
    EffectiveConfigViewModel,
    TokenUsageViewModel,
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


def render_help_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, HelpViewModel):
        return False
    stop_stream_and_clear(renderer)
    if model.diagnostic:
        renderer.console.print(f"[yellow]{model.diagnostic}[/yellow]")
        return True
    table = Table(title="Commands", show_header=True)
    table.add_column("Feature")
    table.add_column("Command")
    table.add_column("Description")
    for section in model.sections:
        for index, command in enumerate(section.commands):
            table.add_row(
                section.feature_id if index == 0 else "",
                command.usage,
                command.description,
            )
    renderer.console.print(table)
    return True


def render_model_profiles_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, ModelListViewModel):
        return False
    stop_stream_and_clear(renderer)
    renderer.console.print(
        f"Main: {model.active_main or model.current_model}  "
        f"Sub: {model.active_sub or 'inherits main'}"
    )
    table = Table(title="Model Profiles", show_header=True)
    table.add_column("Profile")
    table.add_column("Route")
    table.add_column("Model")
    table.add_column("Context")
    for profile in model.profiles:
        routes = "/".join(
            name
            for name, active in (
                ("main", profile.active_main),
                ("sub", profile.active_sub),
            )
            if active
        )
        table.add_row(
            profile.name,
            routes or "-",
            profile.model,
            str(profile.max_context_tokens),
        )
    renderer.console.print(table)
    for diagnostic in model.diagnostics:
        renderer.console.print(f"[yellow]⚠ {diagnostic}[/yellow]")
    return True


def render_modes_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, ModesViewModel):
        return False
    stop_stream_and_clear(renderer)
    table = Table(title=f"Modes · active={model.active_mode or 'none'}")
    table.add_column("Mode")
    table.add_column("Description")
    table.add_column("Tools")
    table.add_column("Subagents")
    for mode in model.modes:
        table.add_row(
            f"{mode.name}{' *' if mode.active else ''}",
            mode.description,
            ", ".join(mode.tools) or "-",
            ", ".join(mode.allowed_subagent_modes) or "-",
        )
    renderer.console.print(table)
    return True


def render_token_usage_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, TokenUsageViewModel):
        return False
    stop_stream_and_clear(renderer)
    table = Table(title="Token Usage", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Prompt", str(model.prompt_tokens))
    table.add_row("Completion", str(model.completion_tokens))
    table.add_row("Lifetime", str(model.lifetime_total))
    context = f"{model.current_context_tokens}/{model.max_context_tokens}"
    if model.context_percent is not None:
        context += f" ({model.context_percent:.1f}%)"
    table.add_row("Current context", context)
    table.add_row("Messages", str(model.message_count))
    table.add_row(
        "Compression hits",
        f"snip={model.snip_hit_count}, summarize={model.summarize_hit_count}",
    )
    renderer.console.print(table)
    return True


def builtin_cli_view_specs() -> list[ViewRendererSpec]:
    """Return explicit CLI-owned view registrations."""
    markdown_views = {
        "approval_rules": "Approval Rules",
        "skills": "Skills",
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
            ViewRendererSpec(view_type="help", render=render_help_view),
            ViewRendererSpec(
                view_type="model_profiles", render=render_model_profiles_view
            ),
            ViewRendererSpec(view_type="mode_profiles", render=render_modes_view),
            ViewRendererSpec(
                view_type="token_usage", render=render_token_usage_view
            ),
        ]
    )
    return specs
