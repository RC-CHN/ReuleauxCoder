"""Integration hook assembly; the domain only owns hook contracts and execution."""

from reuleauxcoder.domain.hooks.discovery import HookSpec


def discover_hook_specs() -> list[HookSpec]:
    from reuleauxcoder.extensions.hooks.builtin import builtin_hook_specs

    return list(builtin_hook_specs())
