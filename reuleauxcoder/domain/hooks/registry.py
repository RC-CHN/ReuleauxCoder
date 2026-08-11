"""Hook registry and execution runtime."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, cast

from reuleauxcoder.domain.hooks.base import (
    GuardHook,
    HookBase,
    ObserverHook,
    TransformHook,
)
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    GuardDecision,
    HookContext,
    HookContextSnapshot,
    HookDiagnostic,
    HookKind,
    HookPoint,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


@dataclass(frozen=True, slots=True)
class _HookFailureFacts:
    phase: str
    error_type: str
    code: str | None = None
    ref: str | None = None


class HookExecutionError(RuntimeError):
    """Safe terminal failure raised for a failed transform hook."""

    def __init__(self, diagnostic: HookDiagnostic) -> None:
        self.phase = diagnostic.phase or diagnostic.hook_point.value
        self.hook_name = diagnostic.hook_name
        self.hook_kind = diagnostic.hook_kind.value
        self.error_type = diagnostic.error_type or "Exception"
        self.code = diagnostic.code
        self.ref = diagnostic.ref
        super().__init__(diagnostic.message)


def _safe_fact(value: object, *, fallback: str | None, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return fallback
    if not value.isascii() or not all(
        character.isalnum() or character in {".", "_", "-", ":"} for character in value
    ):
        return fallback
    return value


def _safe_error_attribute(error: BaseException, name: str) -> str | None:
    try:
        value = getattr(error, name, None)
    except Exception:
        return None
    return _safe_fact(value, fallback=None)


def _failure_facts(
    error: BaseException,
    *,
    default_phase: str,
) -> _HookFailureFacts:
    error_type = _safe_error_attribute(error, "error_type") or _safe_fact(
        type(error).__name__, fallback="Exception", limit=64
    )
    return _HookFailureFacts(
        phase=_safe_error_attribute(error, "phase")
        or _safe_fact(default_phase, fallback="hook")
        or "hook",
        error_type=error_type or "Exception",
        code=_safe_error_attribute(error, "code"),
        ref=_safe_error_attribute(error, "ref"),
    )


def _safe_hook_name(name: object) -> str:
    return _safe_fact(name, fallback="unknown_hook") or "unknown_hook"


def _failure_message(
    facts: _HookFailureFacts,
    *,
    hook_name: str,
    hook_kind: HookKind,
) -> str:
    rendered = [
        f"phase={facts.phase}",
        f"error_type={facts.error_type}",
    ]
    if facts.code is not None:
        rendered.append(f"code={facts.code}")
    if facts.ref is not None:
        rendered.append(f"ref={facts.ref}")
    rendered.extend((f"hook={hook_name}", f"hook_kind={hook_kind.value}"))
    return f"Hook execution failed ({', '.join(rendered)})"


def _failure_diagnostic(
    *,
    hook_name: str,
    hook_point: HookPoint,
    hook_kind: HookKind,
    error: BaseException,
    default_phase: str,
    severity: str,
) -> HookDiagnostic:
    facts = _failure_facts(error, default_phase=default_phase)
    safe_hook_name = _safe_hook_name(hook_name)
    return HookDiagnostic(
        hook_name=safe_hook_name,
        hook_point=hook_point,
        hook_kind=hook_kind,
        message=_failure_message(
            facts,
            hook_name=safe_hook_name,
            hook_kind=hook_kind,
        ),
        severity=severity,
        phase=facts.phase,
        error_type=facts.error_type,
        code=facts.code,
        ref=facts.ref,
    )


class HookRegistry:
    """Instance-scoped registry for hook registration and execution."""

    def __init__(
        self,
        *,
        diagnostic_sink: Callable[[HookDiagnostic], None] | None = None,
        performance_monitor: RuntimePerformanceMonitor | None = None,
    ):
        self._hooks: dict[HookPoint, list[HookBase[Any]]] = defaultdict(list)
        self._kind_cache: dict[
            tuple[HookPoint, HookKind], tuple[HookBase[Any], ...]
        ] = {}
        self._diagnostic_sink = diagnostic_sink
        self._diagnostics: list[HookDiagnostic] = []
        self._performance_monitor = performance_monitor

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

    def set_performance_monitor(
        self, monitor: RuntimePerformanceMonitor | None
    ) -> None:
        self._performance_monitor = monitor

    def drain_diagnostics(self) -> tuple[HookDiagnostic, ...]:
        diagnostics = tuple(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics

    def report_diagnostic(self, diagnostic: HookDiagnostic) -> None:
        """Publish a non-fatal hook-runtime failure to the owning Agent."""
        self._diagnostics.append(diagnostic)
        if self._diagnostic_sink is not None:
            try:
                self._diagnostic_sink(diagnostic)
            except Exception as error:
                self._diagnostics.append(
                    _failure_diagnostic(
                        hook_name="diagnostic_sink",
                        hook_point=diagnostic.hook_point,
                        hook_kind=HookKind.OBSERVER,
                        error=error,
                        default_phase="diagnostic_sink",
                        severity="warning",
                    )
                )

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
            failure: HookDiagnostic | None = None
            try:
                with self._hook_measurement(hook, hook_point, HookKind.GUARD):
                    decision = cast(GuardHook[HookContext], hook).run(context)
                    if not isinstance(decision, GuardDecision):
                        raise TypeError("guard hook returned an invalid decision")
            except Exception as error:
                failure = self._report_failure(hook, hook_point, HookKind.GUARD, error)
            if failure is not None:
                decisions.append(GuardDecision.deny(failure.message))
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
            failure: HookDiagnostic | None = None
            try:
                with self._hook_measurement(hook, hook_point, HookKind.TRANSFORM):
                    result = cast(TransformHook[HookContext], hook).run(current)
                    if result is None or not isinstance(result, current.__class__):
                        raise TypeError("transform hook returned an invalid context")
                    if result is not current and isinstance(
                        current, BeforeLLMRequestContext
                    ):
                        current._transfer_dispatch_callbacks_to(
                            cast(BeforeLLMRequestContext, result)
                        )
            except Exception as error:
                failure = self._report_failure(
                    hook, hook_point, HookKind.TRANSFORM, error
                )
            if failure is not None:
                # Raise after leaving the handler so the original exception —
                # and any sensitive text it owns — is not retained as context.
                raise HookExecutionError(failure) from None
            current = result
        return current

    def run_observers(
        self, hook_point: HookPoint, context: HookContext
    ) -> tuple[HookDiagnostic, ...]:
        """Run observers against an immutable snapshot and report failures."""
        observers = self._iter_kind(hook_point, HookKind.OBSERVER)
        if not observers:
            return ()
        diagnostics: list[HookDiagnostic] = []
        try:
            with self._measurement(
                name=f"{hook_point.value}:snapshot",
                hook_name="observer_snapshot",
                hook_point=hook_point,
                hook_kind=HookKind.OBSERVER,
                attributes={
                    "hook_point": hook_point.value,
                    "hook_kind": HookKind.OBSERVER.value,
                    "observer_count": len(observers),
                },
            ):
                snapshot = self._snapshot(context)
        except Exception as error:
            diagnostics.append(
                self._report_secondary_failure(
                    hook_name="observer_snapshot",
                    hook_point=hook_point,
                    hook_kind=HookKind.OBSERVER,
                    error=error,
                    default_phase="observer_snapshot",
                )
            )
            return tuple(diagnostics)
        for hook in observers:
            try:
                with self._hook_measurement(hook, hook_point, HookKind.OBSERVER):
                    cast(ObserverHook[HookContext], hook).run(snapshot)
            except Exception as error:
                diagnostic = self._report_failure(
                    hook, hook_point, HookKind.OBSERVER, error
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
        cloned = HookRegistry(
            diagnostic_sink=self._diagnostic_sink,
            performance_monitor=self._performance_monitor,
        )
        for hook_point, hooks in self._hooks.items():
            cloned._hooks[hook_point] = [hook.clone_for_scope(scope) for hook in hooks]
        return cloned

    @contextmanager
    def _measurement(
        self,
        *,
        name: str,
        hook_name: str,
        hook_point: HookPoint,
        hook_kind: HookKind,
        attributes: Mapping[str, str | int | float | bool | None],
    ):
        monitor = self._performance_monitor
        if monitor is None:
            yield
            return
        started = time.monotonic()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            try:
                monitor.record(
                    "hook",
                    name,
                    (time.monotonic() - started) * 1000,
                    status=status,
                    attributes=attributes,
                )
            except Exception as error:
                self._report_secondary_failure(
                    hook_name=hook_name,
                    hook_point=hook_point,
                    hook_kind=hook_kind,
                    error=error,
                    default_phase="performance_measurement",
                )

    def _hook_measurement(
        self,
        hook: HookBase[Any],
        hook_point: HookPoint,
        hook_kind: HookKind,
    ):
        hook_name = _safe_hook_name(hook.name)
        return self._measurement(
            name=f"{hook_point.value}:{hook_name}",
            hook_name=hook_name,
            hook_point=hook_point,
            hook_kind=hook_kind,
            attributes={
                "hook_name": hook_name,
                "hook_point": hook_point.value,
                "hook_kind": hook_kind.value,
            },
        )

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
                "_dispatch_callbacks",
                "_dispatch_payload_changed",
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
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return frozenset(cls._freeze(item) for item in value)
        return value

    def _report_failure(
        self,
        hook: HookBase[Any],
        hook_point: HookPoint,
        hook_kind: HookKind,
        error: Exception,
    ) -> HookDiagnostic:
        diagnostic = _failure_diagnostic(
            hook_name=hook.name,
            hook_point=hook_point,
            hook_kind=hook_kind,
            error=error,
            default_phase=hook_point.value,
            severity="error" if hook_kind is not HookKind.OBSERVER else "warning",
        )
        self.report_diagnostic(diagnostic)
        return diagnostic

    def _report_secondary_failure(
        self,
        *,
        hook_name: str,
        hook_point: HookPoint,
        hook_kind: HookKind,
        error: Exception,
        default_phase: str,
    ) -> HookDiagnostic:
        diagnostic = _failure_diagnostic(
            hook_name=hook_name,
            hook_point=hook_point,
            hook_kind=hook_kind,
            error=error,
            default_phase=default_phase,
            severity="warning",
        )
        self.report_diagnostic(diagnostic)
        return diagnostic
