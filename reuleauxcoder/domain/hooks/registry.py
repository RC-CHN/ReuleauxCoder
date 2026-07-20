"""Hook registry and execution runtime."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import fields
from types import MappingProxyType
from typing import Any, cast

from reuleauxcoder.domain.hooks.base import (
    GuardHook,
    HookBase,
    ObserverHook,
    TransformHook,
)
from reuleauxcoder.domain.hooks.types import (
    GuardDecision,
    HookContext,
    HookContextSnapshot,
    HookDiagnostic,
    HookKind,
    HookPoint,
)


class HookRegistry:
    """Instance-scoped registry for hook registration and execution."""

    def __init__(
        self,
        *,
        diagnostic_sink: Callable[[HookDiagnostic], None] | None = None,
    ):
        self._hooks: dict[HookPoint, list[HookBase[Any]]] = defaultdict(list)
        self._kind_cache: dict[
            tuple[HookPoint, HookKind], tuple[HookBase[Any], ...]
        ] = {}
        self._diagnostic_sink = diagnostic_sink
        self._diagnostics: list[HookDiagnostic] = []

    def register(self, hook_point: HookPoint, hook: HookBase[Any]) -> None:
        """Register a hook for a hook point."""
        self._hooks[hook_point].append(hook)
        self._invalidate_hook_cache(hook_point)

    def unregister(self, hook_point: HookPoint, hook_name: str) -> None:
        """Remove a hook by name from a hook point."""
        self._hooks[hook_point] = [
            h for h in self._hooks.get(hook_point, []) if h.name != hook_name
        ]
        self._invalidate_hook_cache(hook_point)

    def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
        """List registered hook names."""
        if hook_point is not None:
            return {
                hook_point.value: [
                    h.name for h in self._sorted_hooks(self._hooks.get(hook_point, []))
                ]
            }
        return {
            point.value: [h.name for h in self._sorted_hooks(hooks)]
            for point, hooks in self._hooks.items()
        }

    def hooks_at(self, hook_point: HookPoint) -> tuple[HookBase[Any], ...]:
        """Return an ordered, read-only view without exposing registry storage."""
        return tuple(self._sorted_hooks(self._hooks.get(hook_point, [])))

    def set_diagnostic_sink(
        self, sink: Callable[[HookDiagnostic], None] | None
    ) -> None:
        self._diagnostic_sink = sink

    def drain_diagnostics(self) -> tuple[HookDiagnostic, ...]:
        diagnostics = tuple(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics

    def bind_runtime_service(self, name: str, service: Any | None) -> None:
        """Bind one scoped service without exposing registry internals."""
        for hooks in self._hooks.values():
            for hook in hooks:
                hook.bind_runtime_service(name, service)

    def run_guards(
        self, hook_point: HookPoint, context: HookContext
    ) -> list[GuardDecision]:
        """Run guard hooks with fail-closed semantics."""
        decisions: list[GuardDecision] = []
        for hook in self._iter_kind(hook_point, HookKind.GUARD):
            try:
                decision = cast(GuardHook[HookContext], hook).run(context)
            except Exception as exc:
                self._report_failure(hook, hook_point, HookKind.GUARD, exc)
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

    def run_transforms(
        self, hook_point: HookPoint, context: HookContext
    ) -> HookContext:
        """Run transform hooks, requiring same-type context results."""
        current = context
        for hook in self._iter_kind(hook_point, HookKind.TRANSFORM):
            try:
                result = cast(TransformHook[HookContext], hook).run(current)
            except Exception as exc:
                self._report_failure(hook, hook_point, HookKind.TRANSFORM, exc)
                raise
            if result is None:
                error = TypeError(
                    f"transform hook '{hook.name}' returned None for {hook_point.value}"
                )
                self._report_failure(hook, hook_point, HookKind.TRANSFORM, error)
                raise error
            if not isinstance(result, current.__class__):
                error = TypeError(
                    f"transform hook '{hook.name}' returned {type(result).__name__}, "
                    f"expected {current.__class__.__name__}"
                )
                self._report_failure(hook, hook_point, HookKind.TRANSFORM, error)
                raise error
            current = result
        return current

    def run_observers(
        self, hook_point: HookPoint, context: HookContext
    ) -> tuple[HookDiagnostic, ...]:
        """Run observers against an immutable snapshot and report failures."""
        observers = self._iter_kind(hook_point, HookKind.OBSERVER)
        if not observers:
            return ()
        snapshot = self._snapshot(context)
        diagnostics: list[HookDiagnostic] = []
        for hook in observers:
            try:
                cast(ObserverHook[HookContext], hook).run(snapshot)
            except Exception as exc:
                diagnostic = self._report_failure(
                    hook, hook_point, HookKind.OBSERVER, exc
                )
                diagnostics.append(diagnostic)
                continue
        return tuple(diagnostics)

    def _iter_kind(
        self, hook_point: HookPoint, kind: HookKind
    ) -> tuple[HookBase[Any], ...]:
        cache_key = (hook_point, kind)
        cached = self._kind_cache.get(cache_key)
        if cached is not None:
            return cached
        hooks = self._sorted_hooks(self._hooks.get(hook_point, []))
        if kind is HookKind.GUARD:
            selected = tuple(h for h in hooks if isinstance(h, GuardHook))
        elif kind is HookKind.TRANSFORM:
            selected = tuple(h for h in hooks if isinstance(h, TransformHook))
        else:
            selected = tuple(h for h in hooks if isinstance(h, ObserverHook))
        self._kind_cache[cache_key] = selected
        return selected

    def _invalidate_hook_cache(self, hook_point: HookPoint) -> None:
        for kind in HookKind:
            self._kind_cache.pop((hook_point, kind), None)

    def clone(self, *, scope: str = "child") -> "HookRegistry":
        """Create a scope-aware copy of the registry and registered hooks."""
        cloned = HookRegistry(diagnostic_sink=self._diagnostic_sink)
        for hook_point, hooks in self._hooks.items():
            cloned._hooks[hook_point] = [hook.clone_for_scope(scope) for hook in hooks]
        return cloned

    @staticmethod
    def _sorted_hooks(hooks: list[HookBase[Any]]) -> list[HookBase[Any]]:
        return sorted(hooks, key=lambda hook: hook.priority, reverse=True)

    @classmethod
    def _snapshot(cls, context: HookContext) -> HookContextSnapshot:
        payload = {
            item.name: cls._freeze(getattr(context, item.name))
            for item in fields(context)
            if item.name
            not in {
                "hook_point",
                "agent_id",
                "session_generation",
                "session_id",
                "turn_id",
                "trace_id",
                "metadata",
            }
        }
        return HookContextSnapshot(
            hook_point=context.hook_point,
            agent_id=context.agent_id,
            session_generation=context.session_generation,
            session_id=context.session_id,
            turn_id=context.turn_id,
            trace_id=context.trace_id,
            metadata=cls._freeze(context.metadata),
            payload=MappingProxyType(payload),
        )

    @classmethod
    def _freeze(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return MappingProxyType(
                {key: cls._freeze(item) for key, item in value.items()}
            )
        if isinstance(value, list):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, set):
            return frozenset(cls._freeze(item) for item in value)
        return value

    def _report_failure(
        self,
        hook: HookBase[Any],
        hook_point: HookPoint,
        hook_kind: HookKind,
        error: Exception,
    ) -> HookDiagnostic:
        diagnostic = HookDiagnostic(
            hook_name=hook.name,
            hook_point=hook_point,
            hook_kind=hook_kind,
            message=str(error),
            severity="error" if hook_kind is not HookKind.OBSERVER else "warning",
        )
        self._diagnostics.append(diagnostic)
        if self._diagnostic_sink is not None:
            self._diagnostic_sink(diagnostic)
        return diagnostic
