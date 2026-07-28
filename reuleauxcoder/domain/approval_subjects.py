"""Stable resource identities used by approval policy rules."""

from __future__ import annotations

import json
from pathlib import Path
import posixpath
from typing import Any

from reuleauxcoder.domain.approval import ApprovalGrantScope
from reuleauxcoder.domain.workspace import WorkspaceError


def canonical_workspace_subject(workspace: Any, path: str) -> str | None:
    """Return one stable, displayable path identity without reading the target.

    Workspace-local paths are relative to the configured root and always use
    forward slashes. External local paths remain normalized absolute paths.
    The workspace adapter owns resolution so existing symlinks and the nearest
    existing parent of a new file follow the same boundary semantics as the
    eventual operation.
    """
    if workspace is None or not isinstance(path, str) or not path:
        return None
    try:
        inspect_external = getattr(workspace, "external_path", None)
        external = inspect_external(path) if callable(inspect_external) else None
        resolved_value = external if external is not None else workspace.resolve(path)
        if not isinstance(resolved_value, (str, Path)):
            return None
        resolved = Path(resolved_value)
        root = Path(workspace.root)
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return _portable_path(resolved)
        value = relative.as_posix()
        return value if value and value != "." else "."
    except (WorkspaceError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _portable_path(path: Path) -> str:
    """Render host and remote path spellings with stable separators."""
    return str(path).replace("\\", "/")


def approval_scope_key(tool: Any, *, session_id: str | None) -> str:
    """Bind reusable grants to one rcoder session and execution environment."""
    backend = getattr(tool, "backend", None)
    context = getattr(backend, "context", None)
    workspace = getattr(backend, "workspace", None)
    root = getattr(workspace, "root", None)
    payload = {
        "backend": getattr(tool, "backend_id", "unknown"),
        "execution_target": getattr(context, "execution_target", None),
        "peer_id": getattr(context, "peer_id", None),
        "session_id": session_id,
        "workspace_root": (
            str(root).replace("\\", "/") if root is not None else None
        ),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_approval_grant_scopes(
    subjects: tuple[str, ...],
) -> tuple[ApprovalGrantScope, ...]:
    """Offer exact file grants and one safe common workspace subtree."""
    if not subjects:
        return ()
    noun = "file" if len(subjects) == 1 else f"{len(subjects)} files"
    scopes = [
        ApprovalGrantScope(
            id="exact",
            label=f"This {noun}",
            description=", ".join(subjects),
            patterns=subjects,
        )
    ]
    if any(_is_absolute_subject(subject) for subject in subjects):
        return tuple(scopes)
    parents = tuple(posixpath.dirname(subject) or "." for subject in subjects)
    try:
        common = posixpath.commonpath(parents)
    except ValueError:
        return tuple(scopes)
    if common in {"", "."}:
        return tuple(scopes)
    pattern = common.rstrip("/") + "/**"
    scopes.append(
        ApprovalGrantScope(
            id="directory",
            label="This directory",
            description=pattern,
            patterns=(pattern,),
            broad=True,
        )
    )
    return tuple(scopes)


def _is_absolute_subject(subject: str) -> bool:
    return (
        subject.startswith("/")
        or (len(subject) >= 3 and subject[1] == ":" and subject[2] == "/")
        or subject.startswith("//")
    )


__all__ = [
    "approval_scope_key",
    "canonical_workspace_subject",
    "file_approval_grant_scopes",
]
