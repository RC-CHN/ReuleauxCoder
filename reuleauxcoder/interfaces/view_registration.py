"""Decorator-based structured view module registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from reuleauxcoder.interfaces.view_registry import ViewRenderer, ViewRendererSpec


@dataclass(frozen=True, slots=True)
class RegisteredViewRenderer:
    """One declarative view renderer registration."""

    view_type: str
    ui_targets: frozenset[str]
    render: ViewRenderer


_REGISTERED_VIEWS: list[RegisteredViewRenderer] = []


def register_view(
    *, view_type: str, ui_targets: set[str] | frozenset[str]
) -> Callable[[ViewRenderer], ViewRenderer]:
    """Register a structured view renderer for one or more UI targets."""

    targets = frozenset(ui_targets)

    def decorator(func: ViewRenderer) -> ViewRenderer:
        registration = RegisteredViewRenderer(
            view_type=view_type, ui_targets=targets, render=func
        )
        if registration not in _REGISTERED_VIEWS:
            _REGISTERED_VIEWS.append(registration)
        return func

    return decorator


def iter_registered_views(
    *, ui_target: str | None = None
) -> Iterator[RegisteredViewRenderer]:
    """Iterate registered views, optionally filtered by UI target."""
    for registration in _REGISTERED_VIEWS:
        if ui_target is None or ui_target in registration.ui_targets:
            yield registration


def build_view_specs(*, ui_target: str) -> list[ViewRendererSpec]:
    """Build view specs for one UI target."""
    return [
        ViewRendererSpec(view_type=registration.view_type, render=registration.render)
        for registration in iter_registered_views(ui_target=ui_target)
    ]
