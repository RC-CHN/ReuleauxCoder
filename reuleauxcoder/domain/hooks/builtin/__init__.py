"""Built-in hook implementations."""

from reuleauxcoder.domain.hooks.builtin.tool_output import ToolOutputTruncationHook
from reuleauxcoder.domain.hooks.builtin.tool_policy import ToolPolicyGuardHook
from reuleauxcoder.domain.hooks.builtin.project_context import (
    ProjectContextHook,
    ProjectContextStartupNotifier,
)
from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import LspEditObserverHook
from reuleauxcoder.domain.hooks.builtin.lsp_injector import LspDiagnosticsInjectorHook

__all__ = [
    "ToolOutputTruncationHook",
    "ToolPolicyGuardHook",
    "ProjectContextHook",
    "ProjectContextStartupNotifier",
    "LspEditObserverHook",
    "LspDiagnosticsInjectorHook",
]
