"""Production terminal UI interface."""

from reuleauxcoder.interfaces.tui.application import MiniTUIApplication
from reuleauxcoder.interfaces.tui.style import (
    MINI_TUI_MOUSE_SUPPORT,
    MINI_TUI_STYLE,
)
from reuleauxcoder.interfaces.tui.event_adapter import MiniTUIEventAdapter
from reuleauxcoder.interfaces.tui.interaction import MiniTUIInteractor

__all__ = [
    "MINI_TUI_MOUSE_SUPPORT",
    "MINI_TUI_STYLE",
    "MiniTUIEventAdapter",
    "MiniTUIInteractor",
    "MiniTUIApplication",
]
