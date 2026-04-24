"""Shared structured view renderer registry primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from reuleauxcoder.interfaces.events import UIEvent


class ViewRenderer(Protocol):
    """Callable protocol for UI-specific structured view renderers."""

    def __call__(self, host: Any, event: UIEvent) -> bool: ...


@dataclass(frozen=True, slots=True)
class ViewRendererSpec:
    """Registration entry for one structured view type."""

    view_type: str
    render: ViewRenderer


class ViewRendererRegistry:
    """Registry mapping semantic view types to UI renderers."""

    def __init__(self, specs: list[ViewRendererSpec]):
        self._renderers = {spec.view_type: spec for spec in specs}

    def get(self, view_type: str) -> ViewRendererSpec | None:
        """Return the renderer spec for a view type if registered."""
        return self._renderers.get(view_type)

    def has(self, view_type: str) -> bool:
        """Return whether a renderer exists for the given view type."""
        return view_type in self._renderers
