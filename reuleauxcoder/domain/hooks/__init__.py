"""Hook runtime - core hook abstractions and registry."""

from reuleauxcoder.domain.hooks.base import (
    HookBase,
    GuardHook,
    ObserverHook,
    TransformHook,
)
from reuleauxcoder.domain.hooks.discovery import (
    HookSpec,
    register_hook,
    discover_hook_specs,
    instantiate_hooks,
    clear_hook_specs,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookContext,
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
    "register_hook",
    "discover_hook_specs",
    "instantiate_hooks",
    "clear_hook_specs",
    "AfterLLMResponseContext",
    "AfterToolExecuteContext",
    "BeforeLLMRequestContext",
    "BeforeToolExecuteContext",
    "GuardDecision",
    "HookContext",
    "HookKind",
    "HookPoint",
    "RunnerShutdownContext",
    "RunnerStartupContext",
    "SessionSaveContext",
    "SessionStartContext",
]
