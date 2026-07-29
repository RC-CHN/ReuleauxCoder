"""Workspace-aware construction of adapter-neutral approval previews."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import cast

from reuleauxcoder.domain.approval import (
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSection,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.diff import build_tool_diff, build_unified_diff
from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceDocumentSnapshot,
    WorkspaceRevision,
    WorkspacePort,
)


@dataclass(frozen=True, slots=True)
class ApprovalDocumentSnapshot:
    path: str
    content: str | None
    revision: WorkspaceRevision

    def same_content(self, other: "ApprovalDocumentSnapshot") -> bool:
        return self.path == other.path and self.revision.same_content(other.revision)


def capture_approval_document(
    request: ApprovalRequest, *, workspace: WorkspacePort | None
) -> ApprovalDocumentSnapshot | None:
    """Capture the complete target document used to detect approval staleness."""
    if workspace is None or request.tool_name not in {"edit_file", "write_file"}:
        return None
    return capture_workspace_document(
        request.tool_name,
        request.tool_args,
        workspace=workspace,
    )


def capture_workspace_document(
    tool_name: str,
    tool_args: dict,
    *,
    workspace: WorkspacePort | None,
) -> ApprovalDocumentSnapshot | None:
    """Capture one mutation target for approval and execution revision binding."""
    if workspace is None or tool_name not in {"edit_file", "write_file"}:
        return None
    file_path = tool_args.get("file_path")
    if not isinstance(file_path, str):
        return None
    resolved = str(workspace.resolve(file_path))
    snapshot = getattr(workspace, "snapshot_text", None)
    if callable(snapshot):
        document = cast(WorkspaceDocumentSnapshot, snapshot(file_path))
        return ApprovalDocumentSnapshot(
            path=document.resolved_path,
            content=document.content,
            revision=document.revision,
        )
    try:
        content = workspace.read_text(file_path)
    except WorkspaceError as error:
        if error.code is not WorkspaceErrorCode.NOT_FOUND:
            raise
        content = None
    encoded = content.encode("utf-8") if content is not None else None
    revision = WorkspaceRevision(
        exists=encoded is not None,
        sha256=hashlib.sha256(encoded).hexdigest() if encoded is not None else None,
        size_bytes=len(encoded) if encoded is not None else 0,
    )
    return ApprovalDocumentSnapshot(
        path=resolved,
        content=content,
        revision=revision,
    )


def diff_approval_documents(
    before: ApprovalDocumentSnapshot, after: ApprovalDocumentSnapshot
) -> str:
    """Describe user/editor changes made while an approval was pending."""
    return build_unified_diff(
        before.content or "",
        after.content or "",
        fromfile=f"before-approval/{before.path}",
        tofile=f"after-approval/{after.path}",
    )


def build_approval_preview(
    request: ApprovalRequest,
    *,
    workspace: WorkspacePort | None,
) -> ApprovalPreview:
    """Build the review payload against the Tool's actual workspace view."""
    boundary = _workspace_boundary_section(request)
    diff = _build_diff(request, workspace=workspace)
    if diff is not None:
        title = (
            "Proposed file diff"
            if request.tool_name == "write_file"
            else "Proposed edit diff"
        )
        return ApprovalPreview(
            sections=boundary
            + (
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
            sections=boundary
            + (
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
            sections=boundary
            + (
                ApprovalSection(
                    id="args",
                    title="Arguments",
                    kind=ApprovalSectionKind.JSON,
                    content=dict(request.tool_args),
                ),
            )
        )
    return ApprovalPreview(sections=boundary)


def _workspace_boundary_section(
    request: ApprovalRequest,
) -> tuple[ApprovalSection, ...]:
    target = request.metadata.get("external_workspace_path")
    if not isinstance(target, str) or not target:
        return ()
    root = request.metadata.get("workspace_root")
    root_line = f"\nWorkspace root: {root}" if isinstance(root, str) and root else ""
    return (
        ApprovalSection(
            id="workspace_boundary",
            title="Outside workspace",
            kind=ApprovalSectionKind.TEXT,
            content=(
                f"Exact external target: {target}{root_line}\n"
                "Approval grants this tool call access to this file only."
            ),
        ),
    )


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
        return f"{args.get('pattern', '')} · under {args.get('path', '.')}{include}"
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
    result = build_tool_diff(old, new, filename, context=context).unified
    return result or None
