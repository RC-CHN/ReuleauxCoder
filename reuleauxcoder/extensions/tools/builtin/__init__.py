"""Explicit builtin tool contributions."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.builtin.control import (
    ReportProgressTool,
    ReportToParentTool,
    RequestGuidanceTool,
    UpdatePlanTool,
)
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.history import (
    ArtifactReadTool,
    HistoryReadTool,
    HistorySearchTool,
)
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.extensions.tools.builtin.notes import (
    DeleteNoteTool,
    EditNoteTool,
    WriteNoteTool,
)
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellSessionTool, ShellTool
from reuleauxcoder.extensions.tools.builtin.subagent_control import (
    InterruptAgentTool,
    ListAgentsTool,
    SendMessageTool,
    SpawnAgentTool,
    WaitAgentTool,
)
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool

_BUILTIN_TOOL_TYPES: tuple[type[Tool], ...] = (
    UpdatePlanTool,
    ReportProgressTool,
    ReportToParentTool,
    RequestGuidanceTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    HistorySearchTool,
    HistoryReadTool,
    ArtifactReadTool,
    ListFileTool,
    LspTool,
    WriteNoteTool,
    EditNoteTool,
    DeleteNoteTool,
    ReadFileTool,
    ShellTool,
    ShellSessionTool,
    SpawnAgentTool,
    SendMessageTool,
    ListAgentsTool,
    WaitAgentTool,
    InterruptAgentTool,
    WriteFileTool,
)


def builtin_tool_types() -> tuple[type[Tool], ...]:
    """Return builtin tool types in stable model-schema order."""
    return _BUILTIN_TOOL_TYPES


__all__ = ["builtin_tool_types"]
