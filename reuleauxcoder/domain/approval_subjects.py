"""Stable resource identities used by approval policy rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


__all__ = ["canonical_workspace_subject"]
