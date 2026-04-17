"""ReuleauxCoder - A terminal-native coding agent framework.

Reinventing the wheel, but only for those who prefer it non-circular.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("reuleauxcoder")
except PackageNotFoundError:
    __version__ = "0.0.0"

from reuleauxcoder.domain.agent import Agent
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.tools.registry import ALL_TOOLS, build_tools

__all__ = ["Agent", "LLM", "Config", "ALL_TOOLS", "build_tools", "__version__"]
