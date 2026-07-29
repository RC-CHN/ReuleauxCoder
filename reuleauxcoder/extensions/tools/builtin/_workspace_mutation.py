"""Shared projection of verified workspace mutation facts."""

from __future__ import annotations

from dataclasses import replace

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceMutationReceipt,
    WorkspaceMutationVerification,
    WorkspaceRevision,
)


def current_expected_revision(backend) -> WorkspaceRevision | None:
    current = getattr(backend, "current_workspace_revision", None)
    revision = current() if callable(current) else None
    return revision if isinstance(revision, WorkspaceRevision) else None


def project_successful_mutation(
    outcome: ToolOutcome,
    receipt: WorkspaceMutationReceipt,
    *,
    operation: str,
) -> ToolOutcome:
    """Attach structured facts and make exceptional states model-visible."""
    warning = _successful_mutation_warning(receipt, operation=operation)
    metadata = {
        **outcome.metadata,
        "mutation_receipt": receipt.to_dict(),
        "mutation_verification": receipt.verification.value,
        "external_change_before_write": receipt.external_change_before_write,
        "atomic_replace": receipt.atomic_replace,
    }
    projected = replace(outcome, metadata=metadata)
    if receipt.verification is WorkspaceMutationVerification.DIVERGED:
        projected = replace(
            projected,
            status=ToolOutcomeStatus.FAILED,
            error_kind=ToolErrorKind.EXECUTION,
        )
    if not warning:
        return projected
    projected = replace(
        projected,
        content=_append(projected.content or projected.summary or "", warning),
    )
    return projected.with_model_projection(projected.model_text)


def workspace_mutation_failure(
    error: WorkspaceError,
    *,
    operation: str,
    file_path: str,
) -> ToolOutcome:
    kind = (
        ToolErrorKind.NOT_FOUND
        if error.code is WorkspaceErrorCode.NOT_FOUND
        else ToolErrorKind.EXECUTION
    )
    content = f"Error [{error.code.value}]: {error.message}"
    metadata: dict[str, object] = {"workspace_error_code": error.code.value}
    receipt = error.mutation_receipt
    if receipt is not None:
        content = _append(
            content,
            _failed_mutation_notice(
                receipt,
                operation=operation,
                file_path=file_path,
            ),
        )
        metadata.update(
            {
                "mutation_receipt": receipt.to_dict(),
                "mutation_verification": receipt.verification.value,
                "external_change_before_write": (
                    receipt.external_change_before_write
                ),
                "atomic_replace": receipt.atomic_replace,
            }
        )
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=content,
        model_content=content if receipt is not None else None,
        error_kind=kind,
        metadata=metadata,
    )


def _successful_mutation_warning(
    receipt: WorkspaceMutationReceipt,
    *,
    operation: str,
) -> str:
    lines: list[str] = []
    expected = receipt.expected_before
    observed = receipt.observed_after

    if receipt.external_change_before_write:
        if operation == "edit":
            lines.extend(
                [
                    "Warning: the file changed externally after this edit was "
                    "prepared. The requested exact replacement was reapplied to "
                    "the latest contents.",
                    f"Prepared revision: {_short(expected)}",
                    f"Applied against: {receipt.before.short_sha256}",
                ]
            )
        else:
            lines.extend(
                [
                    "Warning: the file changed externally after this write was "
                    "prepared. write_file performed the requested full-file "
                    "replacement and overwrote that newer revision.",
                    f"Prepared revision: {_short(expected)}",
                    f"Overwritten revision: {receipt.before.short_sha256}",
                ]
            )

    if not receipt.atomic_replace:
        lines.append(
            "Warning: the backend did not confirm an atomic replacement for this write."
        )

    if receipt.verification is WorkspaceMutationVerification.APPLIED_VERIFIED:
        if lines:
            lines.append(f"Verified result: {_short(observed)}")
    elif receipt.verification is WorkspaceMutationVerification.DIVERGED:
        lines.extend(
            [
                "The target does not match the intended contents after the write.",
                f"Intended revision: {receipt.intended_after_sha256[:12]}",
                f"Observed revision: {_short(observed)}",
                f"Read {receipt.resolved_path} before making further edits.",
            ]
        )
    elif receipt.verification is WorkspaceMutationVerification.UNKNOWN:
        lines.extend(
            [
                "The target's final contents could not be verified.",
                f"Intended revision: {receipt.intended_after_sha256[:12]}",
                f"Read {receipt.resolved_path} before making further edits.",
            ]
        )
    return "\n".join(lines)


def _failed_mutation_notice(
    receipt: WorkspaceMutationReceipt,
    *,
    operation: str,
    file_path: str,
) -> str:
    observed = receipt.observed_after
    if receipt.verification is WorkspaceMutationVerification.APPLIED_VERIFIED:
        return "\n".join(
            [
                f"The {operation} reported an error, but the intended contents are "
                "visible and were verified.",
                f"Before: {receipt.before.short_sha256}",
                f"Verified result: {_short(observed)}",
                "Do not retry blindly; inspect the file if later work depends on it.",
            ]
        )
    if receipt.verification is WorkspaceMutationVerification.FAILED_UNCHANGED:
        return "\n".join(
            [
                "The target was re-checked and still matches its pre-write contents.",
                f"Unchanged revision: {receipt.before.short_sha256}",
            ]
        )
    if receipt.verification is WorkspaceMutationVerification.DIVERGED:
        return "\n".join(
            [
                "The target changed while the write was failing. It may have been "
                "partially modified or changed externally.",
                f"Before: {receipt.before.short_sha256}",
                f"Observed after failure: {_short(observed)}",
                f"Read {file_path} before making further edits.",
            ]
        )
    return "\n".join(
        [
            "The target's final state could not be verified after the write failed.",
            f"Before: {receipt.before.short_sha256}",
            f"Read {file_path} before making further edits.",
        ]
    )


def _short(revision: WorkspaceRevision | None) -> str:
    return revision.short_sha256 if revision is not None else "unknown"


def _append(content: str, notice: str) -> str:
    return f"{content}\n\n{notice}" if content else notice
