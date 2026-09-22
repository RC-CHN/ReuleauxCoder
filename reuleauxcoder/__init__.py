"""ReuleauxCoder - A terminal-native coding agent framework.

Reinventing the wheel, but only for those who prefer it non-circular.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("reuleauxcoder")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["Agent", "LLM", "Config", "build_tools", "__version__"]


def __getattr__(name):
    # Importing a protocol client must not initialize the runtime or its tools.
    from importlib import import_module

    modules = {
        "Agent": "reuleauxcoder.domain.agent",
        "LLM": "reuleauxcoder.services.llm.client",
        "Config": "reuleauxcoder.domain.config.models",
        "build_tools": "reuleauxcoder.extensions.tools.registry",
    }
    if name in modules:
        return getattr(import_module(modules[name]), name)
    raise AttributeError(name)
