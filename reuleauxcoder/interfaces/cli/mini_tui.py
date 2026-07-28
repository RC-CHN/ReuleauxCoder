"""Compatibility imports for the production terminal UI.

New code should import from :mod:`reuleauxcoder.interfaces.tui`.
"""

# Private helpers are intentionally re-exported for downstream compatibility.
# ruff: noqa: F401

from reuleauxcoder.interfaces.tui.application import (
    ALTERNATE_SCROLL_DISABLE,
    ALTERNATE_SCROLL_ENABLE,
    MINI_TUI_MOUSE_SUPPORT,
    MINI_TUI_STYLE,
    MiniTUIEventAdapter,
    MiniTUIInteractor,
    MiniTUIApplication,
    _approval_fragments,
    _cell_fragments,
    _clip,
    _coalesce_stream_events,
    _compact_panel_tail,
    _decorate_transcript_fragments,
    _execution_panel_rows,
    _first_meaningful_line,
    _fit_display,
    _fit_styled_row,
    _format_effective_config_view,
    _format_help_view,
    _format_sessions_view,
    _format_subagent_jobs_view,
    _format_thinking_effort_view,
    _format_token_usage_view,
    _fragments_to_visual_lines,
    _interaction_lines,
    _labeled_panel_row,
    _panel_agent_text,
    _rstrip_fragment_newlines,
    _stream_event_key,
    _view_text,
    _wrap_fragments,
    _wrapped_row_count,
)
from reuleauxcoder.interfaces.tui.interaction import (
    cancelled_response as _cancelled_response,
    interaction_response as _interaction_response,
)

__all__ = [
    "ALTERNATE_SCROLL_DISABLE",
    "ALTERNATE_SCROLL_ENABLE",
    "MINI_TUI_MOUSE_SUPPORT",
    "MINI_TUI_STYLE",
    "MiniTUIEventAdapter",
    "MiniTUIInteractor",
    "MiniTUIApplication",
]
