"""Entrypoint module for ReuleauxCoder interfaces.

This module provides shared initialization logic that can be reused
by different interfaces (CLI, TUI, VSCode extension, etc.).
"""

__all__ = ["AppRunner", "AppContext", "AppOptions", "AppDependencies"]


def __getattr__(name):
    if name in __all__:
        from reuleauxcoder.interfaces.entrypoint import runner

        return getattr(runner, name)
    raise AttributeError(name)
