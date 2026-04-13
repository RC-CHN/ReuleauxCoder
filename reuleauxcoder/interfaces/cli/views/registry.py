"""CLI structured view renderer registry."""

from __future__ import annotations

from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry, ViewRendererSpec
from reuleauxcoder.interfaces.cli.views.list_views import render_mcp_servers_view, render_sessions_view
from reuleauxcoder.interfaces.cli.views.markdown_views import (
    render_approval_rules_view,
    render_help_view,
    render_mode_profiles_view,
    render_model_profiles_view,
    render_token_usage_view,
)


CLI_VIEW_RENDERER_SPECS = [
    ViewRendererSpec(view_type="help", render=render_help_view),
    ViewRendererSpec(view_type="model_profiles", render=render_model_profiles_view),
    ViewRendererSpec(view_type="mode_profiles", render=render_mode_profiles_view),
    ViewRendererSpec(view_type="sessions", render=render_sessions_view),
    ViewRendererSpec(view_type="approval_rules", render=render_approval_rules_view),
    ViewRendererSpec(view_type="token_usage", render=render_token_usage_view),
    ViewRendererSpec(view_type="mcp_servers", render=render_mcp_servers_view),
]

CLI_VIEW_REGISTRY = ViewRendererRegistry(CLI_VIEW_RENDERER_SPECS)
