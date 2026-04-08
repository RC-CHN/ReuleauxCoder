"""Hook runtime - core hook abstractions and registry."""

from reuleauxcoder.domain.hooks.base import HookBase, GuardHook, ObserverHook, TransformHook
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
)

__all__ = [
    "HookBase",
    "GuardHook",
    "ObserverHook",
    "TransformHook",
    "HookRegistry",
    "AfterLLMResponseContext",
    "AfterToolExecuteContext",
    "BeforeLLMRequestContext",
    "BeforeToolExecuteContext",
    "GuardDecision",
    "HookContext",
    "HookKind",
    "HookPoint",
]
