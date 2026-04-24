"""Hook discovery and decorator-based registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from reuleauxcoder.domain.hooks.base import HookBase
from reuleauxcoder.domain.hooks.types import HookPoint

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config


# Global registry for decorator-based hook specifications
_HOOK_SPECS: list[HookSpec] = []


@dataclass(slots=True)
class HookSpec:
    """Specification for a hook registered via decorator."""

    hook_class: type[HookBase[Any]]
    hook_point: HookPoint
    priority: int = 0
    factory: Callable[["Config"], HookBase[Any]] | None = None
    enabled_by_default: bool = True


def register_hook(
    hook_point: HookPoint,
    priority: int = 0,
    enabled_by_default: bool = True,
) -> Callable[[type[HookBase[Any]]], type[HookBase[Any]]]:
    """Decorator to register a hook class for auto-discovery.

    The decorated class should either:
    - Have a `create_from_config(config: Config) -> Self` classmethod
    - Or be directly instantiable with no config dependencies

    Usage:
        @register_hook(HookPoint.BEFORE_TOOL_EXECUTE, priority=100)
        class MyGuardHook(GuardHook[BeforeToolExecuteContext]):
            @classmethod
            def create_from_config(cls, config: Config) -> "MyGuardHook":
                return cls(some_option=config.some_option)

            def run(self, context) -> GuardDecision:
                ...
    """

    def decorator(cls: type[HookBase[Any]]) -> type[HookBase[Any]]:
        spec = HookSpec(
            hook_class=cls,
            hook_point=hook_point,
            priority=priority,
            enabled_by_default=enabled_by_default,
        )
        _HOOK_SPECS.append(spec)
        # Set priority as class attribute for HookBase compatibility
        cls.priority = priority
        return cls

    return decorator


def discover_hook_specs() -> list[HookSpec]:
    """Return all hook specs registered via decorator.

    Import builtin hooks module first to ensure decorators are executed.
    """
    # Ensure builtin hooks are imported so decorators run
    from reuleauxcoder.domain.hooks.builtin import (
        ToolOutputTruncationHook,
        ToolPolicyGuardHook,
        ProjectContextHook,
        ProjectContextStartupNotifier,
    )

    return list(_HOOK_SPECS)


def instantiate_hooks(
    specs: list[HookSpec],
    config: "Config",
    include_disabled: bool = False,
) -> list[tuple[HookPoint, HookBase[Any]]]:
    """Instantiate hooks from specs using config.

    Args:
        specs: List of HookSpec to instantiate.
        config: Configuration to pass to factory methods.
        include_disabled: Whether to include hooks with enabled_by_default=False.

    Returns:
        List of (hook_point, hook_instance) tuples ready for registration.
    """
    result: list[tuple[HookPoint, HookBase[Any]]] = []

    for spec in specs:
        if not include_disabled and not spec.enabled_by_default:
            continue

        hook: HookBase[Any]
        if spec.factory is not None:
            hook = spec.factory(config)
        elif hasattr(spec.hook_class, "create_from_config"):
            hook = spec.hook_class.create_from_config(config)
        else:
            # Direct instantiation with priority from spec
            hook = spec.hook_class(
                name=spec.hook_class.__name__, priority=spec.priority
            )

        result.append((spec.hook_point, hook))

    return result


def clear_hook_specs() -> None:
    """Clear all registered hook specs. Used for testing."""
    _HOOK_SPECS.clear()
