"""Tools extension - builtin tools and registry."""

__all__ = ["build_tools"]


def __getattr__(name):
    if name == "build_tools":
        from reuleauxcoder.extensions.tools.registry import build_tools

        return build_tools
    raise AttributeError(name)
