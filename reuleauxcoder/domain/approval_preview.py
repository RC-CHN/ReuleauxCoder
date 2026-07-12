"""Workspace-aware construction of adapter-neutral approval previews."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from reuleauxcoder.domain.approval import (
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSection,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode, WorkspacePort


@dataclass(frozen=True, slots=True)
class ApprovalDocumentSnapshot:
    path: str
    content: str | None


def capture_approval_document(
    request: ApprovalRequest, *, workspace: WorkspacePort | None
) -> ApprovalDocumentSnapshot | None:
    """Capture the complete target document used to detect approval staleness."""
    if workspace is None or request.tool_name not in {"edit_file", "write_file"}:
        return None
    file_path = request.tool_args.get("file_path")
    if not isinstance(file_path, str):
        return None
    resolved = str(workspace.resolve(file_path))
    try:
        content = workspace.read_text(file_path)
    except WorkspaceError as error:
        if error.code is not WorkspaceErrorCode.NOT_FOUND:
            raise
        content = None
    return ApprovalDocumentSnapshot(path=resolved, content=content)


def diff_approval_documents(
    before: ApprovalDocumentSnapshot, after: ApprovalDocumentSnapshot
) -> str:
    """Describe user/editor changes made while an approval was pending."""
    return "".join(
        difflib.unified_diff(
            (before.content or "").splitlines(keepends=True),
            (after.content or "").splitlines(keepends=True),
            fromfile=f"before-approval/{before.path}",
            tofile=f"after-approval/{after.path}",
            n=3,
        )
    )


def build_approval_preview(
    request: ApprovalRequest,
    *,
    workspace: WorkspacePort | None,
) -> ApprovalPreview:
    """Build the review payload against the Tool's actual workspace view."""
    diff = _build_diff(request, workspace=workspace)
    if diff is not None:
        title = (
            "Proposed file diff"
            if request.tool_name == "write_file"
            else "Proposed edit diff"
        )
        return ApprovalPreview(
            sections=(
                ApprovalSection(
                    id="diff",
                    title=title,
                    kind=ApprovalSectionKind.DIFF,
                    content=diff,
                ),
            )
        )
    compact = _read_only_preview(request)
    if compact is not None:
        return ApprovalPreview(
            sections=(
                ApprovalSection(
                    id="target",
                    title="Target",
                    kind=ApprovalSectionKind.TEXT,
                    content=compact,
                ),
            )
        )
    if request.tool_args:
        return ApprovalPreview(
            sections=(
                ApprovalSection(
                    id="args",
                    title="Arguments",
                    kind=ApprovalSectionKind.JSON,
                    content=dict(request.tool_args),
                ),
            )
        )
    return ApprovalPreview()


def _read_only_preview(request: ApprovalRequest) -> str | None:
    args = request.tool_args
    if request.tool_name == "read_file":
        path = args.get("file_path") or args.get("path") or "."
        offset = args.get("offset", 1)
        limit = args.get("limit")
        suffix = f" · from line {offset} · limit {limit}" if limit else ""
        return f"{path}{suffix}"
    if request.tool_name == "glob":
        return f"{args.get('pattern', '*')} · under {args.get('path', '.')}"
    if request.tool_name == "list_file":
        pattern = f" · pattern {args['pattern']}" if args.get("pattern") else ""
        recursive = " · recursive" if args.get("recursive") else ""
        return f"{args.get('path', '.')}{pattern}{recursive}"
    if request.tool_name == "grep":
        include = f" · files {args['include']}" if args.get("include") else ""
        return (
            f"{args.get('pattern', '')} · under {args.get('path', '.')}{include}"
        )
    if request.tool_name == "lsp":
        operation = args.get("operation", "query")
        path = args.get("file_path") or args.get("path") or "."
        return f"{operation} · {path}"
    return None


def _build_diff(
    request: ApprovalRequest,
    *,
    workspace: WorkspacePort | None,
) -> str | None:
    if workspace is None:
        return None
    file_path = request.tool_args.get("file_path")
    if not isinstance(file_path, str):
        return None

    if request.tool_name == "edit_file":
        old_string = request.tool_args.get("old_string")
        new_string = request.tool_args.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return None
        try:
            content = workspace.read_text(file_path)
        except WorkspaceError:
            return None
        if content.count(old_string) != 1:
            return None
        return _unified_diff(
            content,
            content.replace(old_string, new_string, 1),
            str(workspace.resolve(file_path)),
        )

    if request.tool_name == "write_file":
        new_content = request.tool_args.get("content")
        if not isinstance(new_content, str):
            return None
        try:
            old_content = workspace.read_text(file_path)
        except WorkspaceError as error:
            if error.code is not WorkspaceErrorCode.NOT_FOUND:
                return None
            old_content = ""
        return _unified_diff(
            old_content,
            new_content,
            str(workspace.resolve(file_path)),
        )
    return None


def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str | None:
    result = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=context,
        )
    )
    return result or None
