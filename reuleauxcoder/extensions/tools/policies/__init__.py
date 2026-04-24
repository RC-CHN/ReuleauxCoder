"""Tool policies - safety and execution policies."""

from reuleauxcoder.extensions.tools.policies.base import ToolPolicy
from reuleauxcoder.extensions.tools.policies.shell import ShellDangerousCommandPolicy

DEFAULT_TOOL_POLICIES: tuple[ToolPolicy, ...] = (ShellDangerousCommandPolicy(),)

__all__ = [
    "ToolPolicy",
    "ShellDangerousCommandPolicy",
    "DEFAULT_TOOL_POLICIES",
]
