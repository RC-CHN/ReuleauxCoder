"""Tool registry - manages available tools."""

from typing import Optional

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.builtin.agent import AgentTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool

ALL_TOOLS = [
    ShellTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    AgentTool(),
]


def get_tool(name: str) -> Optional[Tool]:
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None
