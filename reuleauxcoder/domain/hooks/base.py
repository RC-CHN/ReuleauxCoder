"""Hook abstract base classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from reuleauxcoder.domain.hooks.types import GuardDecision, HookContext

ContextT = TypeVar("ContextT", bound=HookContext)


@dataclass(slots=True)
class HookBase(Generic[ContextT]):
    """Common metadata shared by all hooks."""

    name: str
    priority: int = 0
    extension_name: str | None = None


class GuardHook(HookBase[ContextT]):
    """Guard hooks decide whether execution may continue."""

    def run(self, context: ContextT) -> GuardDecision:
        raise NotImplementedError


class TransformHook(HookBase[ContextT]):
    """Transform hooks must return a same-type context."""

    def run(self, context: ContextT) -> ContextT:
        raise NotImplementedError


class ObserverHook(HookBase[ContextT]):
    """Observer hooks can inspect execution without mutating control flow."""

    def run(self, context: ContextT) -> None:
        raise NotImplementedError
