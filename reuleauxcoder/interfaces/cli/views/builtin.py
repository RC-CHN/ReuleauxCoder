"""CLI renderers for builtin structured command views."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from reuleauxcoder.interfaces.cli.views.common import (
    stop_stream_and_clear,
)
from reuleauxcoder.interfaces.view_registry import ViewRendererSpec
from reuleauxcoder.app.runtime.approval import ApprovalView
from reuleauxcoder.extensions.skills.models import SkillsViewModel
from reuleauxcoder.app.commands.view_models import (
    HelpViewModel,
    ModelListViewModel,
    ModesViewModel,
    SessionsViewModel,
    SubagentJobsViewModel,
    EffectiveConfigViewModel,
    TokenUsageViewModel,
)
from reuleauxcoder.extensions.mcp.models import MCPServersView


def render_mcp_servers_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, MCPServersView):
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
    sections = (
        ("Extension Graph", model.extension_graph),
        ("Extension Scopes", model.extension_scopes),
        ("LSP Scopes", model.lsp_scopes),
        ("Peer Capabilities", model.peer_capabilities),
        ("Active Jobs", model.active_jobs),
    )
    for title, values in sections:
        if not values:
            continue
        renderer.console.print(f"[bold]{title}[/bold]")
        for value in values:
            renderer.console.print(f"  {value}")
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


def render_approval_rules_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, ApprovalView):
        return False
    stop_stream_and_clear(renderer)
    renderer.console.print(
        f"Default: {model.default_mode} ({model.default_mode_source})"
    )
    rules = Table(title="Approval Rules")
    rules.add_column("Scope")
    rules.add_column("Target")
    rules.add_column("Action")
    rules.add_column("Source")
    for rule in model.rules:
        target = rule.tool_name or rule.mcp_server or rule.tool_source or "all"
        rules.add_row(rule.scope, target, rule.action, rule.source)
    renderer.console.print(rules)

    policies = Table(title="Effective Tool Policies")
    policies.add_column("Tool")
    policies.add_column("Action")
    policies.add_column("Source")
    for policy in model.tool_policies:
        policies.add_row(policy.tool_name, policy.action, policy.source)
    renderer.console.print(policies)
    return True


def render_skills_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, SkillsViewModel):
        return False
    stop_stream_and_clear(renderer)
    summary = model.summary
    renderer.console.print(
        f"Skills: {summary.active} active / {summary.discovered} discovered / "
        f"{summary.disabled} disabled"
    )
    table = Table(title="Skills")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Scope")
    table.add_column("Description")
    for skill in model.skills:
        table.add_row(
            skill.name,
            "enabled" if skill.enabled else "disabled",
            skill.scope,
            skill.description,
        )
    renderer.console.print(table)
    for diagnostic in model.diagnostics:
        color = "yellow" if diagnostic.level == "warning" else "red"
        renderer.console.print(f"[{color}]{diagnostic.message}[/{color}]")
    return True


def render_subagent_jobs_view(renderer, event) -> bool:
    model = event.data.get("view_model")
    if not isinstance(model, SubagentJobsViewModel):
        return False
    stop_stream_and_clear(renderer)
    renderer.console.print(
        f"Explore workers: {model.runtime_parallel_explore}/"
        f"{model.max_parallel_explore}"
    )
    table = Table(title="Sub-agent Jobs")
    table.add_column("Job")
    table.add_column("Status")
    table.add_column("Mode")
    table.add_column("Generation")
    table.add_column("Task")
    for job in model.jobs[:20]:
        table.add_row(
            job.job_id,
            job.status,
            job.mode,
            str(job.generation),
            job.task,
        )
    renderer.console.print(table)
    if len(model.jobs) == 1:
        job = model.jobs[0]
        if job.error:
            renderer.console.print(f"[red]{job.error}[/red]")
        if job.result:
            renderer.console.print(job.result)
    return True


def builtin_cli_view_specs() -> list[ViewRendererSpec]:
    """Return explicit CLI-owned view registrations."""
    specs: list[ViewRendererSpec] = []
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
            ViewRendererSpec(
                view_type="approval_rules", render=render_approval_rules_view
            ),
            ViewRendererSpec(view_type="skills", render=render_skills_view),
            ViewRendererSpec(
                view_type="subagent_jobs", render=render_subagent_jobs_view
            ),
        ]
    )
    return specs
