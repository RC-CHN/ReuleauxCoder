"""Entrypoint module for ReuleauxCoder interfaces.

This module provides shared initialization logic that can be reused
by different interfaces (CLI, TUI, VSCode extension, etc.).
"""

from reuleauxcoder.interfaces.entrypoint.runner import AppRunner, AppContext, AppOptions

__all__ = ["AppRunner", "AppContext", "AppOptions"]