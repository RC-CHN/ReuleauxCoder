"""Built-in hook that injects project-level context files into LLM requests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import threading
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    HookContextSnapshot,
    RunnerStartupContext,
)
from reuleauxcoder.domain.llm.context_messages import synthetic_user_message


# Candidate filenames to search for project context.
# All existing files are loaded and concatenated in this fixed order
# so the KV cache prefix stays stable across requests.
DEFAULT_CONTEXT_FILES = [
    "AGENT.md",
    "AGENTS.md",
    ".agent.md",
    "CLAUDE.md",
    ".claude.md",
]


@dataclass(frozen=True, slots=True)
class ProjectContextLoadFailure:
    """Content-free failure facts safe to expose to the model."""

    phase: str
    error_type: str
    ref: str


@dataclass(frozen=True, slots=True)
class ProjectContextSnapshot:
    """One stable view of loaded instructions and any load failures."""

    parts: tuple[tuple[str, str], ...] = ()
    failures: tuple[ProjectContextLoadFailure, ...] = ()
    retained_last_good: bool = False


class ProjectContextStartupObservationError(RuntimeError):
    """Safe observer failure reported by the hook registry."""

    def __init__(self, *, phase: str, error_type: str, ref: str) -> None:
        self.phase = _safe_observer_phase(phase)
        self.error_type = _safe_fact_token(
            error_type,
            fallback="Exception",
            max_length=64,
        )
        self.ref = _safe_fact_token(
            ref,
            fallback="project_context",
            max_length=128,
        )
        super().__init__(
            "Project context startup observation failed "
            f"(phase={self.phase}, error_type={self.error_type}, ref={self.ref})"
        )


def _safe_fact_token(value: str, *, fallback: str, max_length: int) -> str:
    if (
        not value
        or len(value) > max_length
        or not value.isascii()
        or not all(
            character.isalnum() or character in {".", "_", "-"} for character in value
        )
    ):
        return fallback
    return value


def _safe_observer_phase(phase: str) -> str:
    return phase if phase in {"workspace", "stat", "notify"} else "observe"


def _safe_error_type(error: BaseException) -> str:
    """Return a bounded exception type without exception-controlled text."""
    name = type(error).__name__
    return _safe_fact_token(name, fallback="Exception", max_length=64)


def _safe_context_ref(filename: str, index: int) -> str:
    """Keep simple basenames useful without exposing configured paths."""
    if (
        filename not in {".", ".."}
        and 0 < len(filename) <= 128
        and all(
            character.isascii()
            and (character.isalnum() or character in {".", "_", "-"})
            for character in filename
        )
    ):
        return filename
    return f"context_file_{index + 1}"


class ProjectContextHook(TransformHook[BeforeLLMRequestContext]):
    """Inject project-level context files (AGENT.md, etc.) into messages.

    Searches for project context files in the current working directory
    and injects the content as a tagged synthetic user message after the
    sole system prompt. This keeps the provider-visible system prefix singular.
    """

    def __init__(
        self,
        *,
        context_files: list[str] | None = None,
        priority: int = 50,
    ):
        super().__init__(
            name="project_context", priority=priority, extension_name="core"
        )
        self.context_files = context_files or DEFAULT_CONTEXT_FILES
        self._cache_lock = threading.Lock()
        self._cache_signature: tuple | None = None
        self._cache_snapshot = ProjectContextSnapshot()
        self._cache_rendered: str | None = None

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProjectContextHook":
        """Create hook instance from config."""
        return cls(priority=50)

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        snapshot = self._load_project_context_snapshot()
        insert_at = 1
        if snapshot.parts:
            # Insert after system prompt (index 0), before conversation history.
            # All found files are concatenated in the fixed DEFAULT_CONTEXT_FILES
            # order so the KV cache prefix stays stable.
            context.messages.insert(
                insert_at,
                synthetic_user_message(
                    "project_context",
                    self._format_multi_message(snapshot.parts),
                    source="workspace_instruction_files",
                    attributes={
                        "files": ",".join(filename for filename, _ in snapshot.parts)
                    },
                ),
            )
            insert_at += 1
        if snapshot.failures:
            context.messages.insert(
                insert_at,
                synthetic_user_message(
                    "session_diagnostic",
                    self._format_load_failures(
                        snapshot.failures,
                        retained_last_good=snapshot.retained_last_good,
                    ),
                    source="workspace_instruction_loader",
                    attributes={"failure_count": len(snapshot.failures)},
                ),
            )
        return context

    def clone_for_scope(self, scope: str) -> "ProjectContextHook":
        del scope
        return ProjectContextHook(
            context_files=list(self.context_files), priority=self.priority
        )

    def _load_project_context_snapshot(self) -> ProjectContextSnapshot:
        """Load existing project context files and retain safe failure facts.

        Returns:
            A stable snapshot in DEFAULT_CONTEXT_FILES order. A genuinely
            missing file is omitted; every other expected filesystem or UTF-8
            failure is represented explicitly.
        """
        try:
            cwd = Path.cwd().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            return ProjectContextSnapshot(
                failures=(
                    ProjectContextLoadFailure(
                        phase="workspace",
                        error_type=_safe_error_type(error),
                        ref="working_directory",
                    ),
                )
            )
        with self._cache_lock:
            signature, signature_failures = self._context_signature(cwd)
            if signature == self._cache_signature:
                return self._cache_snapshot

            found: list[tuple[str, str]] = []
            read_failures: list[ProjectContextLoadFailure] = []
            for index, filename in enumerate(self.context_files):
                candidate = cwd / filename
                file_signature = next(
                    (item for item in signature[1] if item[0] == filename),
                    None,
                )
                if file_signature is None or not isinstance(file_signature[1], tuple):
                    continue
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        found.append((filename, content))
                except UnicodeDecodeError as error:
                    read_failures.append(
                        ProjectContextLoadFailure(
                            phase="decode",
                            error_type=_safe_error_type(error),
                            ref=_safe_context_ref(filename, index),
                        )
                    )
                except (OSError, ValueError) as error:
                    read_failures.append(
                        ProjectContextLoadFailure(
                            phase="read",
                            error_type=_safe_error_type(error),
                            ref=_safe_context_ref(filename, index),
                        )
                    )

            # Do not retain a value if a file changed while it was being read.
            verified_signature, verified_failures = self._context_signature(cwd)
            if verified_signature == signature:
                failures = tuple(
                    dict.fromkeys(
                        (*signature_failures, *read_failures, *verified_failures)
                    )
                )
                # Failed reads must be retried even if stat metadata is unchanged.
                if not failures:
                    snapshot = ProjectContextSnapshot(tuple(found))
                    self._cache_signature = signature
                    self._cache_snapshot = snapshot
                    self._cache_rendered = None
                    return snapshot
                has_last_good = (
                    self._cache_signature is not None
                    and self._cache_signature[0] == signature[0]
                )
                retained = self._cache_snapshot.parts if has_last_good else tuple(found)
                return ProjectContextSnapshot(
                    retained,
                    failures,
                    retained_last_good=has_last_good,
                )

            failures = tuple(
                dict.fromkeys(
                    (
                        *signature_failures,
                        *read_failures,
                        *verified_failures,
                        ProjectContextLoadFailure(
                            phase="verify",
                            error_type="ConcurrentModification",
                            ref="workspace_instruction_set",
                        ),
                    )
                )
            )
            has_last_good = (
                self._cache_signature is not None
                and self._cache_signature[0] == signature[0]
            )
            retained = self._cache_snapshot.parts if has_last_good else ()
            return ProjectContextSnapshot(
                retained,
                failures,
                retained_last_good=has_last_good,
            )

    def _context_signature(
        self, cwd: Path
    ) -> tuple[tuple, tuple[ProjectContextLoadFailure, ...]]:
        files: list[tuple[str, tuple[int, int, int, int] | str | None]] = []
        failures: list[ProjectContextLoadFailure] = []
        for index, filename in enumerate(self.context_files):
            try:
                status = (cwd / filename).stat()
            except FileNotFoundError:
                files.append((filename, None))
                continue
            except (OSError, ValueError) as error:
                error_type = _safe_error_type(error)
                files.append((filename, f"error:{error_type}"))
                failures.append(
                    ProjectContextLoadFailure(
                        phase="stat",
                        error_type=error_type,
                        ref=_safe_context_ref(filename, index),
                    )
                )
                continue
            if not stat.S_ISREG(status.st_mode):
                files.append((filename, "error:NonRegularFile"))
                failures.append(
                    ProjectContextLoadFailure(
                        phase="stat",
                        error_type="NonRegularFile",
                        ref=_safe_context_ref(filename, index),
                    )
                )
                continue
            files.append(
                (
                    filename,
                    (
                        status.st_ino,
                        status.st_size,
                        status.st_mtime_ns,
                        status.st_ctime_ns,
                    ),
                )
            )
        return (str(cwd), tuple(files)), tuple(failures)

    def _format_multi_message(self, parts: Sequence[tuple[str, str]]) -> str:
        """Format multiple project context files into a single system message."""
        parts_key = tuple(parts)
        with self._cache_lock:
            if (
                parts_key == self._cache_snapshot.parts
                and self._cache_rendered is not None
            ):
                return self._cache_rendered
        header = (
            "[Project Context]\n"
            "This is project-level context from local file(s) "
            "(e.g. AGENT.md, CLAUDE.md). "
            "It provides project-specific instructions and conventions.\n"
        )
        sections: list[str] = [header]
        for filename, content in parts:
            sections.append(f"--- {filename} ---\n{content}")
        rendered = "\n".join(sections)
        with self._cache_lock:
            if parts_key == self._cache_snapshot.parts:
                self._cache_rendered = rendered
        return rendered

    @staticmethod
    def _format_load_failures(
        failures: Sequence[ProjectContextLoadFailure],
        *,
        retained_last_good: bool,
    ) -> str:
        lines = [
            "Workspace instructions were not fully loaded. "
            "Treat the affected instruction source as unavailable.",
            (
                "instruction_state=last_good_snapshot_retained"
                if retained_last_good
                else "instruction_state=partial_or_unavailable"
            ),
        ]
        lines.extend(
            f"phase={failure.phase} error_type={failure.error_type} ref={failure.ref}"
            for failure in failures
        )
        return "\n".join(lines)


class ProjectContextStartupNotifier(ObserverHook[RunnerStartupContext]):
    """Notify the UI when project context files are found at startup."""

    def __init__(self, *, priority: int = 0):
        super().__init__(
            name="project_context_startup_notifier",
            priority=priority,
            extension_name="core",
        )

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProjectContextStartupNotifier":
        """Create hook instance from config."""
        return cls(priority=0)

    def run(self, context: HookContextSnapshot) -> None:
        try:
            cwd = Path.cwd()
        except (OSError, RuntimeError) as error:
            raise ProjectContextStartupObservationError(
                phase="workspace",
                error_type=_safe_error_type(error),
                ref="working_directory",
            ) from None
        found: list[str] = []
        for index, filename in enumerate(DEFAULT_CONTEXT_FILES):
            candidate = cwd / filename
            try:
                status = candidate.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ProjectContextStartupObservationError(
                    phase="stat",
                    error_type=_safe_error_type(error),
                    ref=_safe_context_ref(filename, index),
                ) from None
            if not stat.S_ISREG(status.st_mode):
                raise ProjectContextStartupObservationError(
                    phase="stat",
                    error_type="NonRegularFile",
                    ref=_safe_context_ref(filename, index),
                )
            found.append(filename)
        if found and (ui_bus := (context.metadata or {}).get("ui_bus")):
            try:
                from reuleauxcoder.interfaces.events import UIEventKind

                names = ", ".join(found)
                ui_bus.info(
                    f"Found project context: {names}",
                    kind=UIEventKind.CONTEXT,
                )
            except Exception as error:
                raise ProjectContextStartupObservationError(
                    phase="notify",
                    error_type=_safe_error_type(error),
                    ref="project_context_notice",
                ) from None

    def clone_for_scope(self, scope: str) -> "ProjectContextStartupNotifier":
        del scope
        return ProjectContextStartupNotifier(priority=self.priority)
