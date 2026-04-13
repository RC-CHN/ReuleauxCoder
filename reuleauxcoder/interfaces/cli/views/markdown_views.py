"""CLI renderers for markdown-backed structured views."""

from __future__ import annotations

from reuleauxcoder.interfaces.events import UIEvent
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel


def render_help_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    markdown_text = payload.get("markdown")
    return isinstance(markdown_text, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown_text,
        title="Help",
    )


def render_model_profiles_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    markdown_text = payload.get("markdown")
    return isinstance(markdown_text, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown_text,
        title="Model Profiles",
    )


def render_mode_profiles_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    markdown_text = payload.get("markdown")
    return isinstance(markdown_text, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown_text,
        title="Modes",
    )


def render_approval_rules_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    markdown_text = payload.get("markdown")
    return isinstance(markdown_text, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown_text,
        title="Approval Rules",
    )


def render_token_usage_view(renderer, event: UIEvent) -> bool:
    payload = event.data.get("payload") or {}
    markdown_text = payload.get("markdown")
    return isinstance(markdown_text, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown_text,
        title="Token Usage",
    )
