"""Hook registry and execution runtime."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from reuleauxcoder.domain.hooks.base import GuardHook, HookBase, ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.types import GuardDecision, HookContext, HookKind, HookPoint


class HookRegistry:
    """Instance-scoped registry for hook registration and execution."""

    def __init__(self):
        self._hooks: dict[HookPoint, list[HookBase[Any]]] = defaultdict(list)

    def register(self, hook_point: HookPoint, hook: HookBase[Any]) -> None:
        """Register a hook for a hook point."""
        self._hooks[hook_point].append(hook)

    def unregister(self, hook_point: HookPoint, hook_name: str) -> None:
        """Remove a hook by name from a hook point."""
        self._hooks[hook_point] = [h for h in self._hooks.get(hook_point, []) if h.name != hook_name]

    def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
        """List registered hook names."""
        if hook_point is not None:
            return {hook_point.value: [h.name for h in self._sorted_hooks(self._hooks.get(hook_point, []))]}
        return {
            point.value: [h.name for h in self._sorted_hooks(hooks)]
            for point, hooks in self._hooks.items()
        }

    def run_guards(self, hook_point: HookPoint, context: HookContext) -> list[GuardDecision]:
        """Run guard hooks with fail-closed semantics."""
        decisions: list[GuardDecision] = []
        for hook in self._iter_kind(hook_point, HookKind.GUARD):
            try:
                decision = cast(GuardHook[HookContext], hook).run(context)
            except Exception as exc:
                decisions.append(
                    GuardDecision.deny(
                        f"guard hook '{hook.name}' failed at {hook_point.value}: {exc}"
                    )
                )
                break
            decisions.append(decision)
            if not decision.allowed:
                break
        return decisions

    def run_transforms(self, hook_point: HookPoint, context: HookContext) -> HookContext:
        """Run transform hooks, requiring same-type context results."""
        current = context
        for hook in self._iter_kind(hook_point, HookKind.TRANSFORM):
            result = cast(TransformHook[HookContext], hook).run(current)
            if result is None:
                raise TypeError(
                    f"transform hook '{hook.name}' returned None for {hook_point.value}"
                )
            if not isinstance(result, current.__class__):
                raise TypeError(
                    f"transform hook '{hook.name}' returned {type(result).__name__}, "
                    f"expected {current.__class__.__name__}"
                )
            current = result
        return current

    def run_observers(self, hook_point: HookPoint, context: HookContext) -> None:
        """Run observer hooks with fail-open semantics."""
        for hook in self._iter_kind(hook_point, HookKind.OBSERVER):
            try:
                cast(ObserverHook[HookContext], hook).run(context)
            except Exception:
                continue

    def _iter_kind(self, hook_point: HookPoint, kind: HookKind) -> list[HookBase[Any]]:
        hooks = self._sorted_hooks(self._hooks.get(hook_point, []))
        if kind is HookKind.GUARD:
            return [h for h in hooks if isinstance(h, GuardHook)]
        if kind is HookKind.TRANSFORM:
            return [h for h in hooks if isinstance(h, TransformHook)]
        return [h for h in hooks if isinstance(h, ObserverHook)]

    @staticmethod
    def _sorted_hooks(hooks: list[HookBase[Any]]) -> list[HookBase[Any]]:
        return sorted(hooks, key=lambda hook: hook.priority, reverse=True)
