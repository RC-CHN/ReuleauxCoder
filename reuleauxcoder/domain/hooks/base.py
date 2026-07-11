"""Hook abstract base classes."""

from __future__ import annotations

from dataclasses import dataclass
from copy import copy
from typing import Any, Generic, TypeVar

from reuleauxcoder.domain.hooks.types import (
    GuardDecision,
    HookContext,
    HookContextSnapshot,
)

ContextT = TypeVar("ContextT", bound=HookContext)


@dataclass(slots=True)
class HookBase(Generic[ContextT]):
    """Common metadata shared by all hooks."""

    name: str
    priority: int = 0
    extension_name: str | None = None

    def clone_for_scope(self, scope: str) -> "HookBase[ContextT]":
        """Create a hook instance for another runtime scope.

        Stateless hooks may use the default shallow copy. Hooks owning runtime
        resources must override this method and explicitly detach or recreate
        those resources for the requested scope.
        """
        return copy(self)

    def bind_runtime_service(self, name: str, service: Any | None) -> None:
        """Bind or detach an optional scoped runtime service."""
        return


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

    def run(self, context: HookContextSnapshot) -> None:
        raise NotImplementedError
