"""Prompt Toolkit style and terminal protocol constants for the TUI."""

from prompt_toolkit.styles import Style


MINI_TUI_STYLE = Style.from_dict(
    {
        "frame.label": "bold #071013 bg:#67e8f9",
        "frame.border": "#64748b",
        "panel.header": "bold #f8fafc bg:#334155",
        "panel.body": "#dbeafe",
        "panel.label": "bold #071013 bg:#67e8f9",
        "panel.label.secondary": "bold #e2e8f0 bg:#334155",
        "panel.label.need": "bold #071013 bg:#ffd75f",
        "panel.value": "#f8fafc",
        "panel.phase": "bold #b5ff72",
        "panel.live": "bold #67e8f9",
        "panel.detail": "#94a3b8",
        "user": "bold #ffffff bg:#5b4bc4",
        "user.label": "bold #ffffff bg:#6d5ce7",
        "assistant.label": "bold #071013 bg:#67e8f9",
        "turn.separator": "#475569",
        "assistant": "#f8fafc",
        "tool": "#67e8f9",
        "muted": "#94a3b8",
        "success": "#b5ff72",
        "warning": "#ffd75f",
        "error": "#ff8193",
        "diff.add": "#d8ffb0 bg:#17351f",
        "diff.del": "#ffd0d7 bg:#3a1720",
        "diff.header": "bold #67e8f9 bg:#102b33",
        "input": "#ffffff bg:#191827",
        "popup": "#8a86a8 bg:#1c1a2e",
        "popup.cmd": "bold #d8d4f0 bg:#1c1a2e",
        "popup.selected": "bold #ffffff bg:#5b4bc4",
        "interaction": "#fff7d6 bg:#332a12",
        "review.border": "bold #ffd75f",
        "review.approved": "bold #67e8f9",
        "review.denied": "bold #ff8193",
        "review.title.pending": "bold #071013 bg:#ffd75f",
        "review.title.approved": "bold #071013 bg:#67e8f9",
        "review.title.denied": "bold #071013 bg:#ff8193",
        "review.body": "#f8fafc",
        "scrollbar.background": "#29434a bg:#101a1e",
        "scrollbar.button": "#071013 bg:#67e8f9",
        "scrollbar.start": "underline",
        "scrollbar.end": "underline",
        "scrollbar.arrow": "bold #071013 bg:#67e8f9",
    }
)

# Terminal-native mouse ownership is intentional: when prompt_toolkit enables
# mouse tracking, ordinary drag selection never reaches Konsole/iTerm/etc.
# Keyboard transcript navigation remains available through PageUp/PageDown and
# Home/End while users retain native selection, copy, and paste behavior.
MINI_TUI_MOUSE_SUPPORT = False
ALTERNATE_SCROLL_ENABLE = "\x1b[?1007h"
ALTERNATE_SCROLL_DISABLE = "\x1b[?1007l"


__all__ = [
    "ALTERNATE_SCROLL_DISABLE",
    "ALTERNATE_SCROLL_ENABLE",
    "MINI_TUI_MOUSE_SUPPORT",
    "MINI_TUI_STYLE",
]
