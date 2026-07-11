"""Structured outcomes produced by tool execution.

Tools still return strings during the migration.  ``ToolOutcome.from_legacy`` is
the single compatibility boundary that turns those strings into a typed result
without changing what is sent back to the language model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ToolErrorKind(str, Enum):
    """Stable error categories for presentation and protocol adapters."""

    DENIED = "denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    INTERRUPTED = "interrupted"
    EXECUTION = "execution"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ToolArtifact:
    """A structured artifact associated with a tool result."""

    kind: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """Canonical result shared by model, presentation, hooks and transports.

    ``model_text`` is the authoritative legacy-compatible text sent to the LLM.
    Presentation limits must never mutate it.  ``display_summary`` is optional:
    when absent the presentation policy may derive a summary from ``model_text``.
    """

    model_text: str
    success: bool = True
    display_summary: str | None = None
    artifacts: tuple[ToolArtifact, ...] = ()
    error_kind: ToolErrorKind | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    truncated: bool = False
    original_length: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_text, str):
            raise TypeError("ToolOutcome.model_text must be a string")
        if self.success and self.error_kind is not None:
            raise ValueError("A successful ToolOutcome cannot have an error_kind")
        if self.original_length is not None and self.original_length < len(
            self.model_text
        ):
            raise ValueError("original_length cannot be shorter than model_text")

    @classmethod
    def from_legacy(
        cls,
        result: str,
        *,
        success: bool = True,
        error_kind: ToolErrorKind | None = None,
    ) -> "ToolOutcome":
        """Adapt the current string-based Tool contract."""
        return cls(model_text=result, success=success, error_kind=error_kind)

    @property
    def display_text(self) -> str:
        """Return the preferred unbounded display source text."""
        return self.display_summary if self.display_summary is not None else self.model_text
