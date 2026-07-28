"""Built-in hook implementations."""

from reuleauxcoder.domain.hooks.discovery import HookSpec
from reuleauxcoder.domain.hooks.types import HookPoint
from reuleauxcoder.domain.hooks.builtin.tool_output import ToolOutputTruncationHook
from reuleauxcoder.domain.hooks.builtin.tool_policy import ToolPolicyGuardHook
from reuleauxcoder.domain.hooks.builtin.project_context import (
    ProjectContextHook,
    ProjectContextStartupNotifier,
)
from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import LspEditObserverHook
from reuleauxcoder.domain.hooks.builtin.lsp_injector import LspDiagnosticsInjectorHook
from reuleauxcoder.domain.hooks.builtin.git_state import GitStateInjectorHook
from reuleauxcoder.domain.hooks.builtin.process_sessions import (
    ProcessSessionInjectorHook,
)

_BUILTIN_HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec(
        hook_class=ToolOutputTruncationHook,
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        priority=0,
    ),
    HookSpec(
        hook_class=ToolPolicyGuardHook,
        hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
        priority=100,
    ),
    HookSpec(
        hook_class=ProjectContextHook,
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        priority=50,
    ),
    HookSpec(
        hook_class=ProjectContextStartupNotifier,
        hook_point=HookPoint.RUNNER_STARTUP,
        priority=0,
    ),
    HookSpec(
        hook_class=LspEditObserverHook,
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        priority=200,
    ),
    HookSpec(
        hook_class=LspDiagnosticsInjectorHook,
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        priority=100,
    ),
    HookSpec(
        hook_class=GitStateInjectorHook,
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        priority=90,
    ),
    HookSpec(
        hook_class=ProcessSessionInjectorHook,
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        priority=80,
    ),
)


def builtin_hook_specs() -> tuple[HookSpec, ...]:
    """Return builtin hook contributions in stable pipeline order."""
    return _BUILTIN_HOOK_SPECS


__all__ = [
    "builtin_hook_specs",
    "ToolOutputTruncationHook",
    "ToolPolicyGuardHook",
    "ProjectContextHook",
    "ProjectContextStartupNotifier",
    "LspEditObserverHook",
    "LspDiagnosticsInjectorHook",
    "GitStateInjectorHook",
    "ProcessSessionInjectorHook",
]
