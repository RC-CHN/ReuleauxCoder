"""Small, verified multi-file patch tool built on existing workspace writes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.approval_subjects import (
    canonical_workspace_subject,
    file_approval_grant_scopes,
)
from reuleauxcoder.domain.diff import build_tool_diff
from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceRevision,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.builtin._workspace_mutation import (
    project_successful_mutation,
    workspace_mutation_failure,
)


@dataclass(frozen=True, slots=True)
class _Hunk:
    old: tuple[str, ...]
    new: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PatchFile:
    path: str
    add: bool
    hunks: tuple[_Hunk, ...]


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    path: str
    old_content: str
    new_content: str
    expected_revision: WorkspaceRevision


def _parse_patch(patch: str) -> list[_PatchFile]:
    if not isinstance(patch, str) or len(patch) > 256_000:
        raise ValueError("patch must be a string of at most 256000 characters")
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("patch must start with *** Begin Patch and end with *** End Patch")
    files: list[_PatchFile] = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        add = header.startswith("*** Add File: ")
        update = header.startswith("*** Update File: ")
        if not add and not update:
            raise ValueError(f"expected Update File or Add File at patch line {index + 1}")
        path = header.split(": ", 1)[1].strip()
        if not path:
            raise ValueError("patch file path must not be empty")
        if any(item.path == path for item in files):
            raise ValueError(f"duplicate patch file: {path}")
        index += 1
        hunks: list[_Hunk] = []
        if add:
            added: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError(f"Add File {path} accepts only + lines")
                added.append(lines[index][1:])
                index += 1
            hunks.append(_Hunk((), tuple(added)))
        else:
            while index < len(lines) - 1 and not lines[index].startswith("*** Update File: ") and not lines[index].startswith("*** Add File: "):
                if not lines[index].startswith("@@"):
                    raise ValueError(f"expected @@ hunk in {path} at patch line {index + 1}")
                index += 1
                old: list[str] = []
                new: list[str] = []
                while index < len(lines) - 1 and not lines[index].startswith("@@") and not lines[index].startswith("*** "):
                    marker, text = lines[index][:1], lines[index][1:]
                    if marker not in {" ", "-", "+"}:
                        raise ValueError(f"invalid hunk line in {path}: {lines[index]!r}")
                    if marker in {" ", "-"}:
                        old.append(text)
                    if marker in {" ", "+"}:
                        new.append(text)
                    index += 1
                if not old:
                    raise ValueError(f"each update hunk in {path} needs context or removed lines")
                if old == new:
                    raise ValueError(f"hunk in {path} does not change anything")
                hunks.append(_Hunk(tuple(old), tuple(new)))
            if not hunks:
                raise ValueError(f"Update File {path} needs at least one @@ hunk")
        if len(hunks) > 200:
            raise ValueError("a file may contain at most 200 hunks")
        files.append(_PatchFile(path, add, tuple(hunks)))
        if len(files) > 20:
            raise ValueError("a patch may contain at most 20 files")
    return files


def _apply_hunks(content: str, hunks: tuple[_Hunk, ...], path: str) -> str:
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    cursor = 0
    for hunk in hunks:
        old = list(hunk.old)
        matches = [
            index
            for index in range(cursor, len(lines) - len(old) + 1)
            if lines[index : index + len(old)] == old
        ]
        if len(matches) != 1:
            reason = "not found" if not matches else "not unique"
            raise ValueError(f"hunk {reason} in {path}; include more exact context")
        start = matches[0]
        lines[start : start + len(old)] = list(hunk.new)
        cursor = start + len(hunk.new)
    suffix = newline if content.endswith(("\n", "\r")) else ""
    return newline.join(lines) + suffix


def _failure(message: str, *, kind: ToolErrorKind = ToolErrorKind.INVALID_ARGUMENTS) -> ToolOutcome:
    return ToolOutcome(status=ToolOutcomeStatus.FAILED, content=f"Error: {message}", error_kind=kind)


class ApplyPatchTool(Tool):
    name = "apply_patch"
    effect_class = "filesystem_mutation"
    description = (
        "Apply one multi-file, multi-hunk patch through verified workspace writes. "
        "Read every target first. Format:\n"
        "*** Begin Patch\n*** Update File: src/a.py\n@@\n context\n-old\n+new\n"
        "@@\n context\n-old two\n+new two\n*** Add File: src/b.py\n+line\n*** End Patch\n"
        "Update hunks need exact unique context and are applied in order. Add File "
        "accepts only + lines and rejects existing targets. expected_revisions maps "
        "patch paths to full SHA-256 hashes when an explicit precondition is needed; "
        "all current snapshots are always checked again before writing. Up to 20 "
        "files and 256000 characters. A mid-patch race reports applied, failed and "
        "unattempted files; the patch is not a global transaction."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "minLength": 1, "maxLength": 256_000},
            "expected_revisions": {
                "type": "object",
                "additionalProperties": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                "description": "Optional full SHA-256 preconditions keyed by exact patch path.",
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def approval_subjects(self, arguments) -> tuple[str, ...]:
        try:
            files = _parse_patch(arguments.get("patch", ""))
        except (TypeError, ValueError):
            return ()
        subjects = []
        for item in files:
            subject = canonical_workspace_subject(self.backend.workspace, item.path)
            if subject is not None:
                subjects.append(subject)
        return tuple(dict.fromkeys(subjects))

    def approval_grant_scopes(self, arguments, subjects):
        del arguments
        return file_approval_grant_scopes(subjects)

    def _prepare(self, patch: str, expected_revisions: dict[str, str] | None) -> list[_PreparedFile]:
        files = _parse_patch(patch)
        expected = expected_revisions or {}
        unknown = set(expected) - {item.path for item in files}
        if unknown:
            raise ValueError(f"expected_revisions has unknown paths: {sorted(unknown)}")
        for value in expected.values():
            if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise ValueError("expected revision hashes must contain 64 hexadecimal characters")
        prepared: list[_PreparedFile] = []
        for item in files:
            snapshot = self.backend.workspace.snapshot_text(item.path)
            if item.add:
                if snapshot.revision.exists:
                    raise ValueError(f"Add File target already exists: {item.path}")
                content = "\n".join(item.hunks[0].new) + "\n"
                expected_revision = WorkspaceRevision(exists=False, sha256=None, size_bytes=0)
                old = ""
            else:
                if snapshot.content is None:
                    raise ValueError(f"Update File target does not exist: {item.path}")
                wanted = expected.get(item.path)
                if wanted is not None and snapshot.revision.sha256 != wanted.lower():
                    raise ValueError(
                        f"expected revision mismatch for {item.path}: "
                        f"wanted {wanted.lower()[:12]}, found {(snapshot.revision.sha256 or 'missing')[:12]}"
                    )
                old = snapshot.content
                content = _apply_hunks(old, item.hunks, item.path)
                expected_revision = snapshot.revision
            prepared.append(_PreparedFile(item.path, old, content, expected_revision))
        return prepared

    def _preflight_validate(self, patch: str, expected_revisions: dict[str, str] | None = None) -> str | None:  # type: ignore[override]
        try:
            self._prepare(patch, expected_revisions)
        except (WorkspaceError, TypeError, ValueError) as error:
            return f"Error: {error}"
        return None

    def execute(self, patch: str, expected_revisions: dict[str, str] | None = None) -> ToolOutcome:  # type: ignore[override]
        return self.run_backend(patch=patch, expected_revisions=expected_revisions)

    @backend_handler("remote_relay")
    def _execute_remote(self, patch: str, expected_revisions: dict[str, str] | None = None) -> ToolOutcome:
        return self._execute_local(patch, expected_revisions)

    @backend_handler("local")
    def _execute_local(self, patch: str, expected_revisions: dict[str, str] | None = None) -> ToolOutcome:
        validation = self._preflight_validate(patch, expected_revisions)
        if validation:
            return _failure(validation.removeprefix("Error: "))
        prepared = self._prepare(patch, expected_revisions)
        completed: list[tuple[_PreparedFile, ToolOutcome, Any]] = []
        for index, item in enumerate(prepared):
            try:
                result = self.backend.workspace.write_text_verified(
                    item.path, item.new_content, expected_revision=item.expected_revision
                )
            except WorkspaceError as error:
                failure = workspace_mutation_failure(error, operation="apply_patch", file_path=item.path)
                remaining = [entry.path for entry in prepared[index + 1 :]]
                content = (
                    f"Patch stopped at {item.path}; {len(completed)} file(s) were already "
                    f"written and {len(remaining)} were not attempted.\n\n{failure.content}"
                )
                return ToolOutcome(
                    status=ToolOutcomeStatus.FAILED,
                    content=content,
                    error_kind=failure.error_kind,
                    metadata={
                        **failure.metadata,
                        "operation": "apply_patch",
                        "applied_files": [entry.path for entry, _, _ in completed],
                        "failed_file": item.path,
                        "unattempted_files": remaining,
                    },
                )
            resolved = self.backend.workspace.resolve(item.path)
            diff = build_tool_diff(item.old_content, item.new_content, str(resolved))
            outcome = project_successful_mutation(
                ToolOutcome(
                    summary=f"Patched {item.path}",
                    content=f"Patched {item.path}",
                    diff=diff,
                    metadata={"operation": "apply_patch", "file_path": item.path, "resolved_path": str(resolved)},
                ),
                result.receipt,
                operation="apply_patch",
            )
            completed.append((item, outcome, result.receipt))

        resolved_paths = [outcome.metadata["resolved_path"] for _, outcome, _ in completed]
        file_results = [
            {
                "file_path": item.path,
                "resolved_path": outcome.metadata["resolved_path"],
                "mutation_receipt": receipt.to_dict(),
                "diff": outcome.diff.unified if outcome.diff else "",
            }
            for item, outcome, receipt in completed
        ]
        content = f"Applied patch to {len(completed)} file(s)\n\n" + "\n\n".join(
            outcome.model_text + ("\n" + outcome.diff.unified if outcome.diff else "")
            for _, outcome, _ in completed
        )
        metadata: dict[str, Any] = {
            "operation": "apply_patch",
            "file_count": len(completed),
            "resolved_paths": resolved_paths,
            "file_results": file_results,
            "show_diff_by_default": True,
        }
        if len(completed) == 1:
            only = completed[0][1]
            metadata.update(only.metadata)
            metadata["resolved_paths"] = resolved_paths
            metadata["file_results"] = file_results
        return ToolOutcome(
            summary=f"Applied patch to {len(completed)} file(s)",
            content=content,
            diff=completed[0][1].diff if len(completed) == 1 else None,
            metadata=metadata,
        )
