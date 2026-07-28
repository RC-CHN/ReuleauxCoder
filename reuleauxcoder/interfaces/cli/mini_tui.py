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
    _cell_fragments,
    _clip,
    _coalesce_stream_events,
    _compact_panel_tail,
    _decorate_transcript_fragments,
    _execution_panel_rows,
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
    _stream_event_key,
    _view_text,
    _wrap_fragments,
    _wrapped_row_count,
)
from reuleauxcoder.interfaces.tui.interaction import (
    cancelled_response as _cancelled_response,
    interaction_response as _interaction_response,
)
from reuleauxcoder.interfaces.tui.formatting import (
    first_meaningful_line as _first_meaningful_line,
    fit_display as _fit_display,
)
from reuleauxcoder.interfaces.tui.transcript import (
    approval_fragments as _approval_fragments,
    rstrip_fragment_newlines as _rstrip_fragment_newlines,
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
