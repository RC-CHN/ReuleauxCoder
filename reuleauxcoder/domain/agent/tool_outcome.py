"""Canonical structured results produced by tool execution.

The outcome owns facts about an execution.  Model and interface adapters derive
their own text projections from those facts, so presentation truncation can
never silently change what is retained for hooks, diagnostics or archives.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping


class ToolOutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ToolErrorKind(str, Enum):
    """Stable error categories for presentation and protocol adapters."""

    DENIED = "denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    INTERRUPTED = "interrupted"
    EXECUTION = "execution"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ToolDiff:
    path: str
    unified: str
    additions: int | None = None
    deletions: int | None = None
    original_chars: int | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ToolDiagnostic:
    path: str
    line: int
    character: int
    message: str
    severity: str
    code: str | int | None = None
    source: str | None = None
    end_line: int | None = None
    end_character: int | None = None


@dataclass(frozen=True, slots=True)
class ToolTruncation:
    """Describes one model-facing retention limit, not a UI preview limit."""

    original_chars: int
    original_lines: int
    retained_chars: int
    retained_lines: int
    strategy: str = "head"


@dataclass(frozen=True, slots=True)
class ToolArchiveReference:
    path: str
    media_type: str = "text/plain"


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Facts shared by the model, presentation, hooks and transports.

    ``content`` is general-purpose output.  Commands should prefer dedicated
    fields such as ``stdout``, ``stderr`` and ``diff`` when those concepts
    apply.  ``model_content`` is the only model-facing override and is used by
    output-retention hooks; it does not destroy any structured source fields.
    """

    status: ToolOutcomeStatus = ToolOutcomeStatus.SUCCEEDED
    summary: str | None = None
    content: str | None = None
    stdout: str = ""
    stderr: str = ""
    diff: ToolDiff | None = None
    diagnostics: tuple[ToolDiagnostic, ...] = ()
    exit_code: int | None = None
    duration_seconds: float | None = None
    truncation: ToolTruncation | None = None
    archive_reference: ToolArchiveReference | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    error_kind: ToolErrorKind | None = None
    model_content: str | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        if self.success and self.error_kind is not None:
            raise ValueError("A successful ToolOutcome cannot have an error_kind")
        if self.truncation is not None and self.model_content is None:
            raise ValueError("truncation requires an explicit model_content projection")

    @property
    def success(self) -> bool:
        return self.status is ToolOutcomeStatus.SUCCEEDED

    @property
    def model_text(self) -> str:
        """Return the independently bounded projection sent back to the LLM."""
        if self.model_content is not None:
            return self.model_content
        return self._detailed_text(include_diagnostics=True)

    @property
    def display_text(self) -> str:
        """Compatibility name for an unbounded standard UI projection."""
        return self.ui_text(include_details=True)

    def ui_text(self, *, include_details: bool) -> str:
        """Project interface text without consulting model retention limits."""
        if not include_details and self.summary:
            return self.summary
        return self._detailed_text(include_diagnostics=include_details)

    def with_model_projection(
        self,
        text: str,
        *,
        truncation: ToolTruncation | None = None,
        archive_reference: ToolArchiveReference | None = None,
    ) -> "ToolOutcome":
        return replace(
            self,
            model_content=text,
            truncation=truncation,
            archive_reference=archive_reference or self.archive_reference,
        )

    def with_metadata(self, **values: object) -> "ToolOutcome":
        """Return an outcome enriched with immutable presentation facts."""
        return replace(self, metadata={**self.metadata, **values})

    def _detailed_text(self, *, include_diagnostics: bool) -> str:
        sections: list[str] = []
        if self.content:
            sections.append(self.content)
        elif self.summary and not (self.stdout or self.stderr or self.diff):
            sections.append(self.summary)
        if self.stdout:
            sections.append(self.stdout)
        if self.stderr:
            sections.append(f"[stderr]\n{self.stderr}")
        if self.diff is not None and self.diff.unified:
            sections.append(self.diff.unified)
        if include_diagnostics and self.diagnostics:
            rendered = ["[diagnostics]"]
            for diagnostic in self.diagnostics:
                location = f"{diagnostic.path}:{diagnostic.line}:{diagnostic.character}"
                code = f" [{diagnostic.code}]" if diagnostic.code is not None else ""
                rendered.append(
                    f"{location}: {diagnostic.severity}{code}: {diagnostic.message}"
                )
            sections.append("\n".join(rendered))
        if self.exit_code not in {None, 0}:
            sections.append(f"[exit code: {self.exit_code}]")
        return "\n".join(section.rstrip() for section in sections if section).strip()

    @classmethod
    def from_legacy(
        cls,
        result: str,
        *,
        success: bool = True,
        error_kind: ToolErrorKind | None = None,
    ) -> "ToolOutcome":
        """Single compatibility boundary for string-returning tools."""
        if not isinstance(result, str):
            raise TypeError("legacy tool result must be a string")
        status = ToolOutcomeStatus.SUCCEEDED
        if not success:
            if error_kind is ToolErrorKind.DENIED:
                status = ToolOutcomeStatus.DENIED
            elif error_kind is ToolErrorKind.INTERRUPTED:
                status = ToolOutcomeStatus.CANCELLED
            else:
                status = ToolOutcomeStatus.FAILED
        return cls(status=status, content=result, error_kind=error_kind)
