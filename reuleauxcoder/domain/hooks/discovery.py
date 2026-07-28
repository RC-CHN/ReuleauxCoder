"""Explicit builtin hook contributions and instantiation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from reuleauxcoder.domain.hooks.base import HookBase
from reuleauxcoder.domain.hooks.types import HookPoint

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

@dataclass(frozen=True, slots=True)
class HookSpec:
    """Declarative hook contribution instantiated by the composition root."""

    hook_class: type[HookBase[Any]]
    hook_point: HookPoint
    priority: int = 0
    factory: Callable[["Config"], HookBase[Any]] | None = None
    enabled_by_default: bool = True

def discover_hook_specs() -> list[HookSpec]:
    """Return explicit builtin hook specs in stable pipeline order."""
    from reuleauxcoder.domain.hooks.builtin import builtin_hook_specs

    return list(builtin_hook_specs())


def instantiate_hooks(
    specs: Sequence[HookSpec],
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
        elif callable(
            create_from_config := getattr(spec.hook_class, "create_from_config", None)
        ):
            created = create_from_config(config)
            if not isinstance(created, HookBase):
                raise TypeError(
                    f"{spec.hook_class.__name__}.create_from_config() "
                    "must return a HookBase"
                )
            hook = created
        else:
            # Direct instantiation with priority from spec
            hook = spec.hook_class(
                name=spec.hook_class.__name__, priority=spec.priority
            )

        result.append((spec.hook_point, hook))

    return result
