"""Tool policies - safety and execution policies."""

from reuleauxcoder.extensions.tools.policies.base import ToolPolicy
from reuleauxcoder.extensions.tools.policies.bash import BashDangerousCommandPolicy

DEFAULT_TOOL_POLICIES: tuple[ToolPolicy, ...] = (
    BashDangerousCommandPolicy(),
)

__all__ = [
    "ToolPolicy",
    "BashDangerousCommandPolicy",
    "DEFAULT_TOOL_POLICIES",
]
