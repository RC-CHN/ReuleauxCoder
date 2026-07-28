"""Hook runtime - core hook abstractions and registry."""

from reuleauxcoder.domain.hooks.base import (
    HookBase,
    GuardHook,
    ObserverHook,
    TransformHook,
)
from reuleauxcoder.domain.hooks.discovery import (
    HookSpec,
    discover_hook_specs,
    instantiate_hooks,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookContext,
    HookContextSnapshot,
    HookDiagnostic,
    HookKind,
    HookPoint,
    RunnerShutdownContext,
    RunnerStartupContext,
    SessionSaveContext,
    SessionStartContext,
)

__all__ = [
    "HookBase",
    "GuardHook",
    "ObserverHook",
    "TransformHook",
    "HookRegistry",
    "HookSpec",
    "discover_hook_specs",
    "instantiate_hooks",
    "AfterLLMResponseContext",
    "AfterToolExecuteContext",
    "BeforeLLMRequestContext",
    "BeforeToolExecuteContext",
    "GuardDecision",
    "HookContext",
    "HookContextSnapshot",
    "HookDiagnostic",
    "HookKind",
    "HookPoint",
    "RunnerShutdownContext",
    "RunnerStartupContext",
    "SessionSaveContext",
    "SessionStartContext",
]
