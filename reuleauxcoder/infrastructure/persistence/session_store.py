"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.context.manager import (
    MESSAGE_TOKEN_KEY,
    ensure_message_token_counts,
    token_count_backend_name,
)
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import (
    ReplayEnvelope,
    RequestEnvelope,
    align_item_provenance,
    validate_provider_message,
    validate_replay_payload,
)
from reuleauxcoder.domain.history import HistoryEvent, HistoryLedger
from reuleauxcoder.domain.llm.context_messages import synthetic_user_message
from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency
from reuleauxcoder.domain.plan import PlanController
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRestoreIssue,
    SessionRuntimeState,
    is_safe_session_preview,
)
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir
from reuleauxcoder.infrastructure.persistence.session_projection import (
    INDEX_DIRECTORY_NAME,
    SessionInventoryProjection,
    SessionProjectionError,
    SessionProjectionRow,
    SessionProjectionSummary,
)

DEFAULT_SESSION_FINGERPRINT = "local"

# Older request_committed events embedded the complete replay on every API
# round. Since every replay contained all preceding messages, that made the
# ledger grow quadratically. The latest complete replay remains authoritative
# in replay.json; request events retain only stable request/replay metadata.
_SESSION_STORAGE_SCHEMA_VERSION = 2
_MAX_REQUEST_RECORDS = 200
_MAX_RESTORE_ISSUES = 8
_MAX_SESSION_ID_CHARS = 128
_MAX_MODEL_NAME_CHARS = 256
_MAX_SAVED_AT_CHARS = 64
_MAX_FINGERPRINT_CHARS = 256
_MAX_PERSISTED_COUNTER = (1 << 63) - 1
_MAX_ARTIFACT_ID_CHARS = 128
_MAX_RUNTIME_STATE_CHARS = 64_000
_MAX_RUNTIME_ITEMS = 256
_MAX_RUNTIME_TEXT_CHARS = 1_024
_UNKNOWN_INVENTORY_MTIME_NS = (1 << 63) - 1
_HISTORY_COMPLETENESS_VALUES = frozenset(
    {
        "complete",
        "degraded",
        "legacy_snapshot_only",
        "legacy_compacted_or_unknown",
    }
)
_SUBAGENT_JOB_STATUSES = frozenset(
    {
        "queued",
        "running",
        "parking",
        "resuming",
        "blocked",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "killed",
        "timed_out",
        "indeterminate",
        "stale",
    }
)
_SUBAGENT_MESSAGE_KINDS = frozenset(
    {
        "reply",
        "milestone",
        "amendment",
        "warning",
        "approval_needed",
        "partial",
        "guidance",
    }
)


def _safe_restore_fact(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    if not value or len(value) > 64 or not value.isascii():
        return fallback
    if not value.replace("_", "").isalnum():
        return fallback
    return value


def _is_bounded_safe_text(value: object, *, max_chars: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_chars
        and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)
    )


def _is_bounded_runtime_text(value: object, *, max_chars: int) -> bool:
    """Validate runtime metadata while permitting its legitimate empty defaults."""
    return (
        isinstance(value, str)
        and len(value) <= max_chars
        and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)
    )


def _is_safe_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and _is_bounded_safe_text(value, max_chars=_MAX_SESSION_ID_CHARS)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value) is not None
        and ".." not in value
    )


def _is_safe_artifact_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_ARTIFACT_ID_CHARS
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is not None
        and ".." not in value
    )


def _validate_strict_utf8_tree(value: object, *, depth: int = 0) -> None:
    """Reject strings JSON can decode but UTF-8 cannot encode losslessly."""
    if depth > 64:
        raise ValueError("JSON tree nesting is too deep")
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            key.encode("utf-8", errors="strict")
            _validate_strict_utf8_tree(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_strict_utf8_tree(child, depth=depth + 1)
        return
    raise TypeError("value is not a JSON-compatible tree")


class SessionRestoreError(RuntimeError):
    """Safe terminal failure for an authoritative session artifact."""

    code = "session_restore_failed"

    def __init__(self, *, phase: str, error_type: str, ref: str) -> None:
        self.phase = _safe_restore_fact(phase, "restore")
        self.error_type = _safe_restore_fact(error_type, "Exception")
        self.ref = _safe_restore_fact(ref, "session_artifact")
        super().__init__(
            "Session restore failed "
            f"(phase={self.phase}, error_type={self.error_type}, ref={self.ref})"
        )


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if (
        not name
        or len(name) > 64
        or not name.isascii()
        or not name.replace("_", "").isalnum()
    ):
        return "Exception"
    return name


def _is_valid_token_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounded_projection_count(value: object) -> int:
    if not _is_valid_token_count(value):
        return 0
    return min(int(value), _MAX_PERSISTED_COUNTER)


def _bounded_projection_length(value: object) -> int:
    if not isinstance(value, list | tuple):
        return 0
    return min(len(value), _MAX_PERSISTED_COUNTER)


class _RestoreIssueCollector:
    """Bound safe issue facts while making capacity loss explicit."""

    def __init__(self, issues=()) -> None:
        self._issues: list[SessionRestoreIssue] = []
        self._omitted = 0
        try:
            iterator = iter(issues)
        except BaseException:
            self._record_invalid_carrier()
            return
        while True:
            try:
                issue = next(iterator)
            except StopIteration:
                break
            except BaseException:
                self._record_invalid_carrier()
                break
            self.add(issue)

    def add(self, issue: object) -> None:
        if not isinstance(issue, SessionRestoreIssue):
            self._record_invalid_carrier()
            return
        self._add_valid(issue)

    def _record_invalid_carrier(self) -> None:
        self._add_valid(
            SessionRestoreIssue(
                phase="restore_issues_validate",
                error_type="SessionRestoreIssuesValidationError",
                ref="restore_issues",
            )
        )

    def _add_valid(self, issue: SessionRestoreIssue) -> None:
        """Add one internally constructed issue without carrier dispatch."""
        occurrences = max(1, issue.count)
        if issue.error_type == "AdditionalIssuesOmitted":
            self._omitted += occurrences
            return
        key = (issue.phase, issue.error_type, issue.ref)
        for index, current in enumerate(self._issues):
            if (current.phase, current.error_type, current.ref) != key:
                continue
            total = max(1, current.count) + occurrences
            self._issues[index] = SessionRestoreIssue(
                phase=current.phase,
                error_type=current.error_type,
                ref=current.ref,
                count=total,
            )
            return
        if len(self._issues) < _MAX_RESTORE_ISSUES:
            self._issues.append(
                SessionRestoreIssue(
                    phase=issue.phase,
                    error_type=issue.error_type,
                    ref=issue.ref,
                    count=occurrences if occurrences > 1 else 0,
                )
            )
            return
        self._omitted += occurrences

    def record(self, phase: str, error_type: str, ref: str) -> None:
        self.add(
            SessionRestoreIssue(
                phase=_safe_restore_fact(phase, "restore"),
                error_type=_safe_restore_fact(error_type, "Exception"),
                ref=_safe_restore_fact(ref, "session_artifact"),
            )
        )

    def facts(self) -> tuple[SessionRestoreIssue, ...]:
        if not self._omitted:
            return tuple(self._issues)
        visible = list(self._issues[: _MAX_RESTORE_ISSUES - 1])
        omitted = self._omitted + sum(
            max(1, issue.count) for issue in self._issues[len(visible) :]
        )
        visible.append(
            SessionRestoreIssue(
                phase="restore_observability",
                error_type="AdditionalIssuesOmitted",
                ref="session_artifacts",
                count=omitted,
            )
        )
        return tuple(visible)


def _collect_persisted_restore_issues(raw_issues: object) -> _RestoreIssueCollector:
    """Keep valid diagnostic facts and make carrier damage observable."""
    collector = _RestoreIssueCollector()
    if isinstance(raw_issues, list):
        for item in raw_issues:
            try:
                collector.add(SessionRestoreIssue.from_dict(item))
            except (TypeError, ValueError):
                collector.record(
                    "restore_issues_validate",
                    "SessionRestoreIssuesValidationError",
                    "restore_issues",
                )
        return collector
    collector.record(
        "restore_issues_validate",
        "SessionRestoreIssuesValidationError",
        "restore_issues",
    )
    return collector


def _record_reconcile_issues(
    collector: _RestoreIssueCollector,
    mutation_counts: dict[str, int],
) -> None:
    """Expose every protocol repair instead of presenting rewritten history as clean."""
    error_types = {
        "synthesized": "SynthesizedToolResult",
        "reordered": "ReorderedToolResult",
        "discarded": "DiscardedToolResult",
        "filled_content": "FilledToolResultContent",
    }
    for mutation, error_type in error_types.items():
        count = mutation_counts.get(mutation, 0)
        if count <= 0:
            continue
        collector.add(
            SessionRestoreIssue(
                phase="message_reconcile",
                error_type=error_type,
                ref="message_history",
                count=count,
            )
        )


@dataclass(frozen=True, slots=True)
class _InventoryFailure:
    issue: SessionRestoreIssue
    rank: tuple[int, float, str]
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class SessionInventoryResult:
    """One immutable inventory scan, including its degraded facts."""

    sessions: tuple[SessionMetadata, ...]
    issues: tuple[SessionRestoreIssue, ...]


@dataclass(frozen=True, slots=True)
class LatestSessionResult:
    """Latest compatible session and issues from the exact same scan."""

    session: SessionMetadata | None
    issues: tuple[SessionRestoreIssue, ...]


@dataclass(frozen=True, slots=True)
class _InventoryScan:
    ranked_sessions: tuple[tuple[tuple[int, float, str], SessionMetadata], ...]
    failures: tuple[_InventoryFailure, ...]
    projection_rows: tuple[SessionProjectionRow, ...] = ()

    @property
    def issues(self) -> tuple[SessionRestoreIssue, ...]:
        return _RestoreIssueCollector(
            failure.issue for failure in self.failures
        ).facts()


@dataclass(frozen=True, slots=True)
class _HistoryLoadResult:
    events: tuple[HistoryEvent, ...]
    issues: tuple[SessionRestoreIssue, ...]
    next_seq_floor: int


def _reject_non_finite_json(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _read_json_object(path: Path, *, ref: str) -> dict:
    """Read one canonical artifact or raise content-free failure facts."""
    failure: tuple[str, str] | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as error:
        failure = (f"{ref}_decode", _safe_error_type(error))
    except OSError as error:
        failure = (f"{ref}_read", _safe_error_type(error))
    if failure is not None:
        raise SessionRestoreError(
            phase=failure[0],
            error_type=failure[1],
            ref=ref,
        ) from None

    failure_type: str | None = None
    try:
        payload = json.loads(text, parse_constant=_reject_non_finite_json)
        _validate_strict_utf8_tree(payload)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
        failure_type = _safe_error_type(error)
    if failure_type is not None:
        raise SessionRestoreError(
            phase=f"{ref}_decode",
            error_type=failure_type,
            ref=ref,
        ) from None
    if not isinstance(payload, dict):
        raise SessionRestoreError(
            phase=f"{ref}_validate",
            error_type="TypeError",
            ref=ref,
        ) from None
    return payload


@dataclass(slots=True)
class _DirectoryWriteCursor:
    initialized: bool = False
    request_ids: set[str] = field(default_factory=set)
    checkpoint_ids: set[str] = field(default_factory=set)


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self._sessions_dir = sessions_dir or get_sessions_dir()
        self._progress = progress
        self._lock = threading.RLock()
        self._write_cursors: dict[str, _DirectoryWriteCursor] = {}
        self._projection = SessionInventoryProjection(self._sessions_dir)
        self._projection_issue: SessionRestoreIssue | None = None

    @property
    def sessions_dir(self) -> Path:
        """Return the underlying session directory."""
        return self._sessions_dir

    def _ensure_sessions_root(self, *, create: bool) -> object | None:
        try:
            status = self._sessions_dir.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                self._sessions_dir.mkdir(parents=True, exist_ok=True)
                status = self._sessions_dir.lstat()
            except OSError as error:
                raise SessionRestoreError(
                    phase="session_discovery",
                    error_type=_safe_error_type(error),
                    ref="session_directory",
                ) from None
        except OSError as error:
            raise SessionRestoreError(
                phase="session_discovery",
                error_type=_safe_error_type(error),
                ref="session_directory",
            ) from None
        if stat.S_ISLNK(status.st_mode):
            raise SessionRestoreError(
                phase="session_discovery",
                error_type="SymbolicLinkError",
                ref="session_directory",
            ) from None
        if not stat.S_ISDIR(status.st_mode):
            raise SessionRestoreError(
                phase="session_discovery",
                error_type="NotADirectoryError",
                ref="session_directory",
            ) from None
        return status

    def _safe_path_status(
        self,
        path: Path,
        *,
        phase: str,
        ref: str,
    ):
        """Return lstat after rejecting symlinks and resolved root escape."""
        root_status = self._ensure_sessions_root(create=False)
        if root_status is None:
            return None
        try:
            status = path.lstat()
        except FileNotFoundError:
            status = None
        except OSError as error:
            raise SessionRestoreError(
                phase=phase,
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None
        if status is not None and stat.S_ISLNK(status.st_mode):
            raise SessionRestoreError(
                phase=phase,
                error_type="SymbolicLinkError",
                ref=ref,
            ) from None
        try:
            root = self._sessions_dir.resolve(strict=True)
            resolved = path.resolve(strict=status is not None)
        except (OSError, RuntimeError) as error:
            raise SessionRestoreError(
                phase=phase,
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None
        if resolved != root and root not in resolved.parents:
            raise SessionRestoreError(
                phase=phase,
                error_type="PathContainmentError",
                ref=ref,
            ) from None
        return status

    def _read_json_artifact(self, path: Path, *, ref: str) -> dict:
        self._safe_path_status(path, phase=f"{ref}_read", ref=ref)
        return _read_json_object(path, ref=ref)

    def _ensure_safe_directory(self, path: Path, *, ref: str) -> None:
        status = self._safe_path_status(path, phase=f"{ref}_write", ref=ref)
        if status is None:
            try:
                path.mkdir()
            except OSError as error:
                raise SessionRestoreError(
                    phase=f"{ref}_write",
                    error_type=_safe_error_type(error),
                    ref=ref,
                ) from None
            status = self._safe_path_status(path, phase=f"{ref}_write", ref=ref)
        if status is None or not stat.S_ISDIR(status.st_mode):
            raise SessionRestoreError(
                phase=f"{ref}_write",
                error_type="NotADirectoryError",
                ref=ref,
            ) from None

    def set_progress_callback(self, progress: Callable[[str], None] | None) -> None:
        self._progress = progress

    def _report_progress(self, message: str) -> None:
        if self._progress is None:
            return
        try:
            self._progress(message)
        except Exception:
            pass

    @property
    def session_projection_path(self) -> Path:
        """Return the disposable SQLite projection path for diagnostics."""
        return self._projection.database_path

    def _record_projection_failure(self, error: BaseException) -> None:
        error_type = (
            error.error_type
            if isinstance(error, SessionProjectionError)
            else _safe_error_type(error)
        )
        self._projection_issue = SessionRestoreIssue(
            phase="session_projection",
            error_type=error_type,
            ref="session_index",
        )
        self._report_progress(
            "Session query projection degraded "
            f"(error_type={error_type}); authoritative artifacts remain available."
        )
        try:
            self._projection.retain_failure(error_type)
        except BaseException:
            return

    def _consume_projection_failure(self) -> None:
        try:
            error_type = self._projection.consume_failure()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_projection_failure(error)
            return
        if error_type is not None:
            self._projection_issue = SessionRestoreIssue(
                phase="session_projection",
                error_type=error_type,
                ref="session_index",
            )

    def _reset_projection_after_failure(self) -> None:
        try:
            self._projection.reset()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_projection_failure(error)

    def _begin_projection_update(self) -> bool:
        try:
            return self._projection.mark_dirty()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_projection_failure(error)
            self._reset_projection_after_failure()
            return False

    def _finish_projection_update(
        self,
        session: Session,
        *,
        projection_was_ready: bool,
    ) -> None:
        if not projection_was_ready:
            return
        manifest_path = self._get_session_directory(session.id) / "manifest.json"
        metadata = SessionMetadata(
            id=session.id,
            model=session.model,
            saved_at=session.saved_at,
            preview=session.get_preview(),
            fingerprint=session.fingerprint,
        )
        try:
            rank = self._metadata_rank(metadata, manifest_path, ref="manifest")
            self._projection.upsert(
                SessionProjectionRow(
                    metadata=metadata,
                    rank_mtime_ns=rank[0],
                    rank_saved_at=rank[1],
                    source_kind="manifest",
                    source_mtime_ns=rank[0],
                    prompt_tokens=session.total_prompt_tokens,
                    completion_tokens=session.total_completion_tokens,
                    event_count=len(session.history_events),
                    request_count=len(session.request_envelopes),
                    checkpoint_count=len(session.checkpoints),
                )
            )
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_projection_failure(error)
            self._reset_projection_after_failure()

    def _with_projection_issue(self, scan: _InventoryScan) -> _InventoryScan:
        issue = self._projection_issue
        if issue is None:
            return scan
        self._projection_issue = None
        return replace(
            scan,
            failures=scan.failures
            + (
                _InventoryFailure(
                    issue=issue,
                    rank=(-1, float("-inf"), "session_index"),
                    blocking=False,
                ),
            ),
        )

    def _inventory_scan(
        self,
        *,
        fingerprint: str | None,
        limit: int,
    ) -> _InventoryScan:
        self._consume_projection_failure()
        try:
            projected = self._projection.query(
                fingerprint=fingerprint,
                limit=limit,
            )
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_projection_failure(error)
            self._reset_projection_after_failure()
            projected = None
        if projected is not None:
            try:
                for row in projected:
                    self._validate_metadata(row.metadata, ref="manifest")
                    self._parse_saved_at(row.metadata.saved_at, ref="manifest")
                    if (
                        row.rank_mtime_ns < 0
                        or not math.isfinite(row.rank_saved_at)
                        or any(
                            value < 0 or value > _MAX_PERSISTED_COUNTER
                            for value in (
                                row.prompt_tokens,
                                row.completion_tokens,
                                row.event_count,
                                row.request_count,
                                row.checkpoint_count,
                            )
                        )
                    ):
                        raise ValueError("invalid session projection row")
            except KeyboardInterrupt:
                raise
            except BaseException:
                self._record_projection_failure(
                    SessionProjectionError("ProjectionRowValidationError")
                )
                self._reset_projection_after_failure()
                projected = None
        if projected is not None:
            return self._with_projection_issue(
                _InventoryScan(
                    ranked_sessions=tuple(
                        (
                            (
                                row.rank_mtime_ns,
                                row.rank_saved_at,
                                row.metadata.id,
                            ),
                            row.metadata,
                        )
                        for row in projected
                    ),
                    failures=(),
                    projection_rows=projected,
                )
            )

        scan = self._scan_inventory(fingerprint=fingerprint)
        if self._ensure_sessions_root(create=False) is None:
            return self._with_projection_issue(scan)
        if scan.failures:
            self._reset_projection_after_failure()
        else:
            try:
                self._report_progress("Rebuilding session query projection...")
                self._projection.replace(scan.projection_rows)
            except KeyboardInterrupt:
                raise
            except BaseException as error:
                self._record_projection_failure(error)
                self._reset_projection_after_failure()
        return self._with_projection_issue(scan)

    def projection_summary(self) -> SessionProjectionSummary | None:
        """Return aggregate counters when the clean projection is available."""
        with self._lock:
            scan = self._inventory_scan(fingerprint=None, limit=1)
            if any(failure.blocking for failure in scan.failures):
                return None
            try:
                return self._projection.summary()
            except KeyboardInterrupt:
                raise
            except BaseException as error:
                self._record_projection_failure(error)
                self._reset_projection_after_failure()
                return None

    def _ensure_message_token_counts(
        self,
        messages: list[dict],
        *,
        reason: str,
    ) -> int:
        pending = 0
        for message in messages:
            if _is_valid_token_count(message.get(MESSAGE_TOKEN_KEY)):
                continue
            message.pop(MESSAGE_TOKEN_KEY, None)
            pending += 1
        if not pending:
            return ensure_message_token_counts(messages)
        self._report_progress(
            f"Calculating token counts for {pending} {reason} message(s)..."
        )
        started = time.monotonic()
        total = ensure_message_token_counts(messages)
        self._report_progress(
            f"Token counts ready ({pending} message(s), "
            f"{token_count_backend_name()}, "
            f"{time.monotonic() - started:.1f}s)."
        )
        return total

    def save(
        self,
        messages: list[dict],
        model: str,
        session_id: Optional[str] = None,
        is_exit: bool = False,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
        history_events: list[HistoryEvent] | tuple[HistoryEvent, ...] | None = None,
        history_next_seq_floor: int = 0,
        replay_envelope: ReplayEnvelope | None = None,
        request_envelopes: list[RequestEnvelope] | tuple[RequestEnvelope, ...] = (),
        history_completeness: str | None = None,
        checkpoints: list[CompactionCheckpoint] | tuple[CompactionCheckpoint, ...] = (),
        restore_issues: list[SessionRestoreIssue]
        | tuple[SessionRestoreIssue, ...] = (),
        incremental: bool = False,
        events_already_persisted: bool = False,
    ) -> str:
        """Save conversation to disk and return the session ID."""
        with self._lock:
            self._ensure_sessions_root(create=True)

            if not session_id:
                session_id = self.generate_session_id()
            self._require_safe_session_id(session_id)

            try:
                _validate_strict_utf8_tree(messages)
                _validate_strict_utf8_tree(model)
                _validate_strict_utf8_tree(fingerprint)
                for message in messages:
                    validate_provider_message(message)
                if runtime_state is not None:
                    _validate_strict_utf8_tree(runtime_state.to_dict())
                    self._validate_runtime_state_payload(runtime_state.to_dict())
                if replay_envelope is not None:
                    _validate_strict_utf8_tree(replay_envelope.to_dict())
            except (TypeError, UnicodeError, ValueError) as error:
                raise SessionRestoreError(
                    phase="session_write_validate",
                    error_type=_safe_error_type(error),
                    ref="session",
                ) from None
            if history_completeness is not None and (
                not isinstance(history_completeness, str)
                or history_completeness not in _HISTORY_COMPLETENESS_VALUES
            ):
                raise SessionRestoreError(
                    phase="session_write_validate",
                    error_type="HistoryCompletenessValidationError",
                    ref="session",
                ) from None
            for request in request_envelopes:
                if not _is_safe_artifact_id(getattr(request, "request_id", None)):
                    raise SessionRestoreError(
                        phase="request_record_validate",
                        error_type="ArtifactIdentityValidationError",
                        ref="request_record",
                    ) from None
                try:
                    _validate_strict_utf8_tree(request.to_dict())
                except (AttributeError, TypeError, UnicodeError, ValueError) as error:
                    raise SessionRestoreError(
                        phase="request_record_write_validate",
                        error_type=_safe_error_type(error),
                        ref="request_record",
                    ) from None
            for checkpoint in checkpoints:
                if not _is_safe_artifact_id(getattr(checkpoint, "id", None)):
                    raise SessionRestoreError(
                        phase="checkpoint_validate",
                        error_type="ArtifactIdentityValidationError",
                        ref="checkpoint",
                    ) from None
                try:
                    _validate_strict_utf8_tree(checkpoint.to_dict())
                except (AttributeError, TypeError, UnicodeError, ValueError) as error:
                    raise SessionRestoreError(
                        phase="checkpoint_write_validate",
                        error_type=_safe_error_type(error),
                        ref="checkpoint",
                    ) from None

            reconcile_counts: dict[str, int] = {}
            saved_messages, _ = reconcile_tool_call_adjacency(
                [dict(message) for message in messages],
                missing_content=lambda _tool_call_id, tool_name: (
                    f"Tool '{tool_name}' interrupted before session persistence."
                ),
                mutation_counts=reconcile_counts,
            )
            restore_issue_collector = _RestoreIssueCollector(restore_issues)
            _record_reconcile_issues(restore_issue_collector, reconcile_counts)
            ensure_message_token_counts(saved_messages)
            exit_events: tuple[HistoryEvent, ...] = ()
            exit_message: dict | None = None
            if is_exit:
                exit_time = time.strftime("%Y-%m-%d %H:%M:%S %Z")
                exit_message = {
                    "role": "user",
                    "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
                }
                ensure_message_token_counts([exit_message])
                saved_messages.append(exit_message)

            ledger = HistoryLedger(
                history_events or (),
                next_seq_floor=history_next_seq_floor,
                session_id=session_id,
            )
            if history_events is None:
                for message in saved_messages:
                    event_count = len(ledger.events)
                    ledger.append_message(message, source="legacy_save_snapshot")
                    if exit_message is not None and message is exit_message:
                        exit_events = ledger.events[event_count:]
            elif is_exit:
                assert exit_message is not None
                event_count = len(ledger.events)
                ledger.append_message(exit_message, source="session_exit")
                exit_events = ledger.events[event_count:]

            effective_runtime = runtime_state or SessionRuntimeState(
                model=model, active_mode=active_mode
            )
            if effective_runtime.model is None:
                effective_runtime.model = model
            if effective_runtime.active_mode is None:
                effective_runtime.active_mode = active_mode

            base_replay = replay_envelope
            try:
                replay = ReplayEnvelope.create(
                    session_id=session_id,
                    cache_epoch=base_replay.cache_epoch if base_replay else 0,
                    history_version=(base_replay.history_version if base_replay else 0),
                    model_profile=(
                        base_replay.model_profile
                        if base_replay
                        else effective_runtime.model or model
                    ),
                    provider_family=(
                        base_replay.provider_family
                        if base_replay
                        else "openai-compatible"
                    ),
                    request_mode=(
                        base_replay.request_mode if base_replay else "chat-completions"
                    ),
                    request_settings=(
                        dict(base_replay.request_settings) if base_replay else {}
                    ),
                    instructions=(
                        list(base_replay.instructions) if base_replay else []
                    ),
                    tools=list(base_replay.tools) if base_replay else [],
                    items=saved_messages,
                    item_provenance=align_item_provenance(
                        saved_messages, ledger.events
                    ),
                )
            except (TypeError, UnicodeError, ValueError) as error:
                raise SessionRestoreError(
                    phase="replay_write_validate",
                    error_type=_safe_error_type(error),
                    ref="replay",
                ) from None
            session = Session(
                id=session_id,
                model=effective_runtime.model or model,
                saved_at=datetime.now().isoformat(timespec="microseconds"),
                fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
                messages=saved_messages,
                active_mode=effective_runtime.active_mode or active_mode,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                runtime_state=effective_runtime,
                history_events=list(ledger.events),
                replay_envelope=replay,
                request_envelopes=list(request_envelopes)[-_MAX_REQUEST_RECORDS:],
                checkpoints=list(checkpoints),
                history_completeness=(
                    history_completeness
                    or (
                        "complete"
                        if history_events is not None
                        else "legacy_compacted_or_unknown"
                    )
                ),
                history_next_seq_floor=ledger.last_sequence,
                restore_issues=restore_issue_collector.facts(),
            )
            try:
                self._validate_metadata(
                    SessionMetadata(
                        id=session.id,
                        model=session.model,
                        saved_at=session.saved_at,
                        preview=session.get_preview(),
                        fingerprint=session.fingerprint,
                    ),
                    ref="manifest",
                )
            except SessionRestoreError as error:
                raise SessionRestoreError(
                    phase="session_write_validate",
                    error_type=error.error_type,
                    ref="session",
                ) from None
            projection_was_ready = self._begin_projection_update()
            self._write_session_directory(
                session,
                incremental=incremental,
                events_already_persisted=events_already_persisted,
                additional_events=(exit_events if events_already_persisted else ()),
            )
            self._finish_projection_update(
                session,
                projection_was_ready=projection_was_ready,
            )
            return session_id

    def append_system_message(
        self,
        session_id: str,
        model: str,
        content: str,
        *,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> None:
        """Append a tagged runtime diagnostic, retaining the legacy API name."""
        appended = synthetic_user_message(
            "session_diagnostic",
            content,
            source="session_store",
        )
        with self._lock:
            loaded = self.load(session_id)
            if loaded is None:
                self.save(
                    messages=[appended],
                    model=model,
                    session_id=session_id,
                    active_mode=active_mode,
                    runtime_state=runtime_state,
                    fingerprint=fingerprint,
                )
                return

            updated_messages = list(loaded.messages)
            updated_messages.append(appended)
            ledger = HistoryLedger(
                loaded.history_events,
                next_seq_floor=loaded.history_next_seq_floor,
            )
            ledger.append_message(appended, source="session_diagnostic")
            self.save(
                messages=updated_messages,
                model=loaded.model or model,
                session_id=session_id,
                total_prompt_tokens=loaded.total_prompt_tokens,
                total_completion_tokens=loaded.total_completion_tokens,
                active_mode=loaded.active_mode or active_mode,
                runtime_state=runtime_state or loaded.runtime_state,
                fingerprint=loaded.fingerprint or fingerprint,
                history_events=list(ledger.events),
                history_next_seq_floor=ledger.last_sequence,
                replay_envelope=loaded.replay_envelope,
                request_envelopes=loaded.request_envelopes,
                checkpoints=loaded.checkpoints,
                history_completeness=loaded.history_completeness,
                restore_issues=loaded.restore_issues,
            )

    @staticmethod
    def generate_session_id() -> str:
        """Generate a new session ID."""
        return f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def load(self, session_id: str) -> Session | None:
        """Load a saved session."""
        with self._lock:
            self._report_progress(f"Loading session files for {session_id}...")
            if self._ensure_sessions_root(create=False) is None:
                return None
            path = self._get_session_path(session_id)
            directory = self._get_session_directory(session_id)
            directory_status = self._safe_path_status(
                directory,
                phase="session_discovery",
                ref="session_directory",
            )
            legacy_status = self._safe_path_status(
                path,
                phase="session_discovery",
                ref="legacy_session",
            )
            if directory_status is None and legacy_status is None:
                return None

            data = None
            if directory_status is not None:
                if not stat.S_ISDIR(directory_status.st_mode):
                    raise SessionRestoreError(
                        phase="session_discovery",
                        error_type="NotADirectoryError",
                        ref="session_directory",
                    )
                session = self._load_session_directory(
                    directory,
                    expected_session_id=session_id,
                )
            else:
                data = self._read_json_artifact(path, ref="legacy_session")
                try:
                    self._validate_legacy_session_payload(data)
                    session = Session.from_dict(data)
                    self._validate_legacy_session_identity(session, session_id)
                    self._validate_metadata(
                        SessionMetadata(
                            id=session.id,
                            model=session.model,
                            saved_at=session.saved_at,
                            preview=session.get_preview(),
                            fingerprint=session.fingerprint,
                        ),
                        ref="legacy_session",
                    )
                    self._parse_saved_at(
                        session.saved_at,
                        ref="legacy_session",
                    )
                except SessionRestoreError:
                    raise
                except Exception as error:
                    error_type = _safe_error_type(error)
                else:
                    error_type = None
                if error_type is not None:
                    raise SessionRestoreError(
                        phase="legacy_session_validate",
                        error_type=error_type,
                        ref="legacy_session",
                    ) from None
                session.history_behavior_projection_safe = (
                    session.history_completeness == "complete"
                )

            if directory_status is not None and legacy_status is not None:
                # A validated canonical directory supersedes the old
                # compatibility snapshot. Keeping both doubles current-state
                # storage and makes it unclear which copy is authoritative.
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    session.restore_issues += (
                        SessionRestoreIssue(
                            phase="legacy_cleanup",
                            error_type=_safe_error_type(error),
                            ref="legacy_session",
                        ),
                    )
            reconcile_counts: dict[str, int] = {}
            updated_messages, _ = reconcile_tool_call_adjacency(
                [dict(message) for message in session.messages],
                missing_content=lambda _tool_call_id, tool_name: (
                    f"Tool '{tool_name}' interrupted in persisted session."
                ),
                mutation_counts=reconcile_counts,
            )
            if any(reconcile_counts.values()):
                collector = _RestoreIssueCollector(session.restore_issues)
                _record_reconcile_issues(collector, reconcile_counts)
                session.restore_issues = collector.facts()
            self._ensure_message_token_counts(
                updated_messages,
                reason="restored",
            )
            session.messages = updated_messages
            if session.runtime_state.model is None:
                session.runtime_state.model = session.model
            if session.runtime_state.active_mode is None:
                session.runtime_state.active_mode = session.active_mode
            if data is not None:
                # Reading a legacy single-file session upgrades it to the
                # canonical directory format. Only remove the duplicate after
                # every canonical artifact has been written successfully.
                self.save(
                    messages=session.messages,
                    model=session.model,
                    session_id=session.id or session_id,
                    total_prompt_tokens=session.total_prompt_tokens,
                    total_completion_tokens=session.total_completion_tokens,
                    active_mode=session.active_mode,
                    runtime_state=session.runtime_state,
                    fingerprint=session.fingerprint,
                    history_events=session.history_events or None,
                    replay_envelope=session.replay_envelope,
                    request_envelopes=session.request_envelopes,
                    history_completeness=session.history_completeness,
                    checkpoints=session.checkpoints,
                    restore_issues=session.restore_issues,
                )
                migrated = self._load_session_directory(
                    directory,
                    expected_session_id=session.id,
                )
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    migrated.restore_issues += (
                        SessionRestoreIssue(
                            phase="legacy_cleanup",
                            error_type=_safe_error_type(error),
                            ref="legacy_session",
                        ),
                    )
                session = migrated
            return session

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        """List available sessions, newest first."""
        return list(self.list_result(limit=limit, fingerprint=fingerprint).sessions)

    def list_result(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> SessionInventoryResult:
        """Return sessions and diagnostics from one projection generation."""
        with self._lock:
            scan = self._inventory_scan(fingerprint=fingerprint, limit=limit)
            selected = tuple(metadata for _, metadata in scan.ranked_sessions[:limit])
            return SessionInventoryResult(sessions=selected, issues=scan.issues)

    def _scan_inventory(self, *, fingerprint: str | None) -> _InventoryScan:
        """Scan manifests once; authoritative replay/history stay lazy."""
        sessions_status = self._ensure_sessions_root(create=False)
        if sessions_status is None:
            return _InventoryScan((), ())

        ranked_sessions: list[tuple[tuple[int, float, str], SessionMetadata]] = []
        projection_rows: list[SessionProjectionRow] = []
        failures: list[_InventoryFailure] = []
        seen_ids: set[str] = set()
        canonical_keys: set[str] = set()

        def source_rank(
            source_path: Path,
        ) -> tuple[tuple[int, float, str], SessionRestoreIssue | None]:
            rank_error_type: str | None = None
            try:
                self._safe_path_status(
                    source_path,
                    phase="inventory_rank",
                    ref=(
                        "manifest"
                        if source_path.name == "manifest.json"
                        else "legacy_session"
                    ),
                )
                modified_ns = source_path.stat().st_mtime_ns
            except SessionRestoreError as error:
                rank_error_type = error.error_type
                modified_ns = _UNKNOWN_INVENTORY_MTIME_NS
            except OSError as error:
                rank_error_type = _safe_error_type(error)
                modified_ns = _UNKNOWN_INVENTORY_MTIME_NS
            source_id = (
                source_path.parent.name
                if source_path.name == "manifest.json"
                else source_path.stem
            )
            # A corrupt entry cannot provide a trusted saved_at value. On an
            # mtime tie, fail closed instead of silently preferring health.
            rank = (modified_ns, float("inf"), source_id)
            if rank_error_type is None:
                return rank, None
            return rank, SessionRestoreIssue(
                phase="inventory_rank",
                error_type=rank_error_type,
                ref=(
                    "manifest"
                    if source_path.name == "manifest.json"
                    else "legacy_session"
                ),
            )

        def record_failure(
            error: SessionRestoreError,
            *,
            source_path: Path,
            rank: tuple[int, float, str] | None = None,
        ) -> None:
            rank_issue = None
            if rank is None:
                if error.phase == "inventory_rank":
                    source_id = (
                        source_path.parent.name
                        if source_path.name == "manifest.json"
                        else source_path.stem
                    )
                    rank = (
                        _UNKNOWN_INVENTORY_MTIME_NS,
                        float("inf"),
                        source_id,
                    )
                    rank_issue = SessionRestoreIssue(
                        phase=error.phase,
                        error_type=error.error_type,
                        ref=error.ref,
                    )
                else:
                    rank, rank_issue = source_rank(source_path)
            failures.append(
                _InventoryFailure(
                    issue=SessionRestoreIssue(
                        phase=error.phase,
                        error_type=error.error_type,
                        ref=error.ref,
                    ),
                    rank=rank,
                )
            )
            if rank_issue is not None and (
                rank_issue.phase,
                rank_issue.error_type,
                rank_issue.ref,
            ) != (error.phase, error.error_type, error.ref):
                failures.append(
                    _InventoryFailure(
                        issue=rank_issue,
                        rank=rank,
                        blocking=False,
                    )
                )

        try:
            entries = list(self._sessions_dir.iterdir())
        except OSError as error:
            raise SessionRestoreError(
                phase="session_discovery",
                error_type=_safe_error_type(error),
                ref="session_directory",
            ) from None

        legacy_files: list[Path] = []
        for entry in sorted(entries, key=lambda path: path.name):
            try:
                entry_status = entry.lstat()
            except FileNotFoundError:
                is_legacy = entry.suffix == ".json"
                record_failure(
                    SessionRestoreError(
                        phase="session_discovery",
                        error_type="FileNotFoundError",
                        ref=("legacy_session" if is_legacy else "session_directory"),
                    ),
                    source_path=entry if is_legacy else entry / "manifest.json",
                )
                continue
            except OSError as error:
                record_failure(
                    SessionRestoreError(
                        phase="session_discovery",
                        error_type=_safe_error_type(error),
                        ref="session_inventory",
                    ),
                    source_path=entry / "manifest.json",
                )
                continue
            if stat.S_ISLNK(entry_status.st_mode):
                is_legacy = entry.suffix == ".json"
                record_failure(
                    SessionRestoreError(
                        phase="session_discovery",
                        error_type="SymbolicLinkError",
                        ref="legacy_session" if is_legacy else "session_directory",
                    ),
                    source_path=entry if is_legacy else entry / "manifest.json",
                )
                continue
            if stat.S_ISREG(entry_status.st_mode) and entry.suffix == ".json":
                legacy_files.append(entry)
                continue
            if not stat.S_ISDIR(entry_status.st_mode):
                continue
            if entry.name == INDEX_DIRECTORY_NAME:
                continue
            manifest_path = entry / "manifest.json"
            if not _is_safe_session_id(entry.name):
                canonical_keys.add(entry.name)
                record_failure(
                    SessionRestoreError(
                        phase="manifest_validate",
                        error_type="SessionIdentityValidationError",
                        ref="manifest",
                    ),
                    source_path=manifest_path,
                )
                continue
            try:
                next(entry.iterdir())
            except StopIteration:
                # Resolving a new ledger path reserves its directory before
                # the first durable event. With no artifacts there is no
                # persisted session to restore or diagnose.
                continue
            except OSError as error:
                canonical_keys.add(entry.name)
                record_failure(
                    SessionRestoreError(
                        phase="session_discovery",
                        error_type=_safe_error_type(error),
                        ref="session_directory",
                    ),
                    source_path=manifest_path,
                )
                continue
            canonical_keys.add(entry.name)
            try:
                metadata, metadata_issues, manifest = self._load_directory_metadata(
                    entry
                )
            except SessionRestoreError as error:
                record_failure(error, source_path=manifest_path)
                continue
            try:
                rank = self._metadata_rank(metadata, manifest_path, ref="manifest")
            except SessionRestoreError as error:
                record_failure(
                    error,
                    source_path=manifest_path,
                )
                continue
            for issue in metadata_issues:
                failures.append(
                    _InventoryFailure(
                        issue=issue,
                        rank=rank,
                        blocking=False,
                    )
                )
            projection_rows.append(
                SessionProjectionRow(
                    metadata=metadata,
                    rank_mtime_ns=rank[0],
                    rank_saved_at=rank[1],
                    source_kind="manifest",
                    source_mtime_ns=rank[0],
                    prompt_tokens=_bounded_projection_count(
                        manifest.get("total_prompt_tokens")
                    ),
                    completion_tokens=_bounded_projection_count(
                        manifest.get("total_completion_tokens")
                    ),
                    event_count=_bounded_projection_count(
                        manifest.get("event_count")
                    ),
                    request_count=_bounded_projection_length(
                        manifest.get("request_ids")
                    ),
                    checkpoint_count=_bounded_projection_length(
                        manifest.get("checkpoint_ids")
                    ),
                )
            )
            seen_ids.add(metadata.id)
            if fingerprint is None or metadata.fingerprint == fingerprint:
                ranked_sessions.append((rank, metadata))

        for file_path in legacy_files:
            if file_path.stem in canonical_keys or file_path.stem in seen_ids:
                continue
            try:
                data = self._read_json_artifact(file_path, ref="legacy_session")
                self._validate_legacy_session_payload(data)
                session = Session.from_dict(data)
                self._validate_legacy_session_identity(session, file_path.stem)
                metadata = SessionMetadata(
                    id=session.id,
                    model=session.model,
                    saved_at=session.saved_at,
                    preview=session.get_preview(),
                    fingerprint=session.fingerprint,
                )
                self._validate_metadata(metadata, ref="legacy_session")
                rank = self._metadata_rank(metadata, file_path, ref="legacy_session")
            except SessionRestoreError as error:
                record_failure(
                    error,
                    source_path=file_path,
                )
                continue
            except Exception as error:
                record_failure(
                    SessionRestoreError(
                        phase="legacy_session_validate",
                        error_type=_safe_error_type(error),
                        ref="legacy_session",
                    ),
                    source_path=file_path,
                )
                continue
            for issue in session.restore_issues:
                failures.append(
                    _InventoryFailure(
                        issue=issue,
                        rank=rank,
                        blocking=False,
                    )
                )
            projection_rows.append(
                SessionProjectionRow(
                    metadata=metadata,
                    rank_mtime_ns=rank[0],
                    rank_saved_at=rank[1],
                    source_kind="legacy",
                    source_mtime_ns=rank[0],
                    prompt_tokens=_bounded_projection_count(
                        session.total_prompt_tokens
                    ),
                    completion_tokens=_bounded_projection_count(
                        session.total_completion_tokens
                    ),
                    event_count=len(session.history_events),
                    request_count=len(session.request_envelopes),
                    checkpoint_count=len(session.checkpoints),
                )
            )
            seen_ids.add(metadata.id)
            if fingerprint is None or metadata.fingerprint == fingerprint:
                ranked_sessions.append((rank, metadata))

        ranked_sessions.sort(key=lambda item: item[0], reverse=True)
        return _InventoryScan(
            tuple(ranked_sessions),
            tuple(failures),
            tuple(projection_rows),
        )

    def get_latest(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> SessionMetadata | None:
        """Return the most recent session metadata, if any."""
        return self.get_latest_result(fingerprint=fingerprint).session

    def get_latest_result(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> LatestSessionResult:
        """Select latest metadata and diagnostics without a last-scan race."""
        with self._lock:
            scan = self._inventory_scan(fingerprint=fingerprint, limit=1)
        latest_rank, latest = (
            scan.ranked_sessions[0]
            if scan.ranked_sessions
            else ((-1, float("-inf"), ""), None)
        )
        relevant_failures = tuple(
            failure for failure in scan.failures if failure.blocking
        )
        newest_failure = max(
            relevant_failures, key=lambda item: item.rank, default=None
        )
        if newest_failure is not None and (
            latest is None or newest_failure.rank >= latest_rank
        ):
            raise SessionRestoreError(
                phase=newest_failure.issue.phase,
                error_type=newest_failure.issue.error_type,
                ref=newest_failure.issue.ref,
            ) from None
        return LatestSessionResult(session=latest, issues=scan.issues)

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        """Extract exit time from persisted session messages, if present."""
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in ("system", "user"):
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            match = re.search(r"\[SESSION_EXIT\].* at (.+?)\.$", content)
            if match:
                return match.group(1)
        return None

    def _get_session_path(self, session_id: str) -> Path:
        """Map session ID to JSON file path."""
        self._require_safe_session_id(session_id)
        return self._sessions_dir / f"{session_id}.json"

    def _get_session_directory(self, session_id: str) -> Path:
        self._require_safe_session_id(session_id)
        return self._sessions_dir / session_id

    def get_session_events_path(self, session_id: str) -> Path:
        """Resolve the durable ledger with the same injective ID policy."""
        self._ensure_sessions_root(create=True)
        directory = self._get_session_directory(session_id)
        self._ensure_safe_directory(directory, ref="session_directory")
        events_path = directory / "events.jsonl"
        self._safe_path_status(
            events_path,
            phase="history_write",
            ref="history_ledger",
        )
        return events_path

    @staticmethod
    def _require_safe_session_id(session_id: object) -> None:
        if _is_safe_session_id(session_id):
            return
        raise SessionRestoreError(
            phase="session_identity",
            error_type="SessionIdentityValidationError",
            ref="session",
        ) from None

    @staticmethod
    def _validate_metadata(metadata: SessionMetadata, *, ref: str) -> None:
        phase = "manifest_validate" if ref == "manifest" else "legacy_session_validate"
        if not _is_bounded_safe_text(
            metadata.fingerprint,
            max_chars=_MAX_FINGERPRINT_CHARS,
        ):
            raise SessionRestoreError(
                phase=phase,
                error_type="SessionFingerprintValidationError",
                ref=ref,
            ) from None
        if (
            not _is_safe_session_id(metadata.id)
            or not _is_bounded_safe_text(
                metadata.model,
                max_chars=_MAX_MODEL_NAME_CHARS,
            )
            or not _is_bounded_safe_text(
                metadata.saved_at,
                max_chars=_MAX_SAVED_AT_CHARS,
            )
            or not is_safe_session_preview(metadata.preview)
        ):
            raise SessionRestoreError(
                phase=phase,
                error_type="SessionMetadataValidationError",
                ref=ref,
            ) from None

    @staticmethod
    def _validate_legacy_session_identity(session: Session, file_stem: str) -> None:
        if session.id != file_stem:
            raise SessionRestoreError(
                phase="legacy_session_validate",
                error_type="SessionIdentityMismatchError",
                ref="legacy_session",
            ) from None

    def _validate_legacy_session_payload(self, payload: dict) -> None:
        def fail(error_type: str = "LegacySessionPayloadValidationError") -> None:
            raise SessionRestoreError(
                phase="legacy_session_validate",
                error_type=error_type,
                ref="legacy_session",
            ) from None

        for key in ("id", "model", "saved_at"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                fail()
        fingerprint = payload.get("fingerprint", DEFAULT_SESSION_FINGERPRINT)
        if not isinstance(fingerprint, str) or not fingerprint:
            fail()
        active_mode = payload.get("active_mode")
        if active_mode is not None and not _is_bounded_safe_text(
            active_mode, max_chars=_MAX_RUNTIME_TEXT_CHARS
        ):
            fail()
        history_completeness = payload.get("history_completeness")
        if history_completeness is not None and (
            not isinstance(history_completeness, str)
            or history_completeness not in _HISTORY_COMPLETENESS_VALUES
        ):
            fail()
        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            fail()
        try:
            for message in messages:
                validate_provider_message(message)
        except (TypeError, ValueError):
            fail("SessionMessageValidationError")
        records = payload.get("history_events", [])
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            fail()
        if "runtime_state" in payload:
            try:
                self._validate_runtime_state_payload(payload["runtime_state"])
            except ValueError:
                fail("SessionRuntimeStateValidationError")
        try:
            previous_seq = 0
            seen_event_ids: set[str] = set()
            for item in payload.get("history_events", ()):
                self._validate_history_event_payload(
                    item,
                    expected_session_id=payload["id"],
                )
                event_id = item["event_id"]
                seq = item["seq"]
                if event_id in seen_event_ids or seq <= previous_seq:
                    raise ValueError("legacy history ordering is invalid")
                seen_event_ids.add(event_id)
                previous_seq = seq
        except (KeyError, TypeError, ValueError):
            fail()

        issue_collector = _collect_persisted_restore_issues(
            payload.get("restore_issues", [])
        )
        replay_payload = payload.get("replay_envelope")
        legacy_replay_invalid = False
        if replay_payload is not None:
            try:
                validate_replay_payload(
                    replay_payload,
                    expected_session_id=payload["id"],
                )
                legacy_replay = ReplayEnvelope.from_dict(replay_payload)
                if (
                    not legacy_replay.validate()
                    or not legacy_replay.validate_protocol()
                ):
                    raise ValueError("legacy replay integrity is invalid")
            except Exception as error:
                legacy_replay_invalid = True
                payload["replay_envelope"] = None
                issue_collector.record(
                    "legacy_replay_validate",
                    _safe_error_type(error),
                    "replay",
                )

        raw_requests = payload.get("request_envelopes", [])
        valid_requests: list[dict] = []
        if legacy_replay_invalid and raw_requests:
            issue_collector.record(
                "legacy_request_validate",
                "ReplayDependencyValidationError",
                "request_record",
            )
        elif not isinstance(raw_requests, list):
            issue_collector.record(
                "legacy_request_validate",
                "RequestRecordValidationError",
                "request_record",
            )
        else:
            for raw_request in raw_requests:
                try:
                    if not isinstance(raw_request, dict):
                        raise TypeError("legacy request must be an object")
                    request_id = raw_request.get("request_id")
                    if not isinstance(request_id, str):
                        raise ValueError("legacy request identity is invalid")
                    self._validate_request_record_payload(
                        raw_request,
                        expected_id=request_id,
                    )
                    RequestEnvelope(**raw_request)
                except Exception as error:
                    issue_collector.record(
                        "legacy_request_validate",
                        _safe_error_type(error),
                        "request_record",
                    )
                    continue
                valid_requests.append(raw_request)
        payload["request_envelopes"] = valid_requests

        raw_checkpoints = payload.get("checkpoints", [])
        valid_checkpoints: list[dict] = []
        if not isinstance(raw_checkpoints, list):
            issue_collector.record(
                "legacy_checkpoint_validate",
                "CheckpointValidationError",
                "checkpoint",
            )
        else:
            for raw_checkpoint in raw_checkpoints:
                try:
                    if not isinstance(raw_checkpoint, dict):
                        raise TypeError("legacy checkpoint must be an object")
                    checkpoint_id = raw_checkpoint.get("id")
                    if not isinstance(checkpoint_id, str):
                        raise ValueError("legacy checkpoint identity is invalid")
                    self._validate_checkpoint_payload(
                        raw_checkpoint,
                        expected_id=checkpoint_id,
                    )
                    CompactionCheckpoint.from_dict(raw_checkpoint)
                except Exception as error:
                    issue_collector.record(
                        "legacy_checkpoint_validate",
                        _safe_error_type(error),
                        "checkpoint",
                    )
                    continue
                valid_checkpoints.append(raw_checkpoint)
        payload["checkpoints"] = valid_checkpoints
        for key in ("total_prompt_tokens", "total_completion_tokens"):
            value = payload.get(key, 0)
            if _is_valid_token_count(value):
                continue
            # Historical API usage cannot be reconstructed exactly from a
            # snapshot. Reset the invalid projection instead of inventing it.
            payload[key] = 0
            issue_collector.record(
                "token_counts_validate",
                "TokenUsageCounterValidationError",
                key,
            )
        for message in messages:
            if MESSAGE_TOKEN_KEY not in message:
                continue
            if _is_valid_token_count(message[MESSAGE_TOKEN_KEY]):
                continue
            message.pop(MESSAGE_TOKEN_KEY, None)
            issue_collector.record(
                "token_counts_validate",
                "MessageTokenMetadataValidationError",
                "message_token_metadata",
            )
        payload["restore_issues"] = [
            issue.to_dict() for issue in issue_collector.facts()
        ]

    @staticmethod
    def _parse_saved_at(saved_at: str, *, ref: str) -> datetime:
        try:
            return datetime.fromisoformat(saved_at)
        except (TypeError, ValueError):
            try:
                return datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError) as error:
                raise SessionRestoreError(
                    phase=(
                        "manifest_validate"
                        if ref == "manifest"
                        else "legacy_session_validate"
                    ),
                    error_type=_safe_error_type(error),
                    ref=ref,
                ) from None

    @staticmethod
    def _validate_runtime_state_payload(runtime: object) -> None:
        if not isinstance(runtime, dict):
            raise ValueError("runtime state must be an object")
        _validate_strict_utf8_tree(runtime)
        try:
            encoded_runtime = json.dumps(
                runtime,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("runtime state is not JSON-compatible") from error
        if len(encoded_runtime) > _MAX_RUNTIME_STATE_CHARS:
            raise ValueError("runtime state exceeds the persisted size limit")
        optional_strings = (
            "model",
            "active_mode",
            "active_main_model_profile",
            "active_sub_model_profile",
            "execution_target",
        )
        for key in optional_strings:
            value = runtime.get(key)
            if value is not None and not _is_bounded_runtime_text(
                value, max_chars=_MAX_RUNTIME_TEXT_CHARS
            ):
                raise ValueError("runtime string field is invalid")
        debug_trace = runtime.get("llm_debug_trace")
        if debug_trace is not None and not isinstance(debug_trace, bool):
            raise ValueError("runtime debug flag is invalid")
        for key in ("remote_binding", "plan_state", "progress_state"):
            if key in runtime and not isinstance(runtime[key], dict):
                raise ValueError("runtime mapping field is invalid")
        skills_disabled = runtime.get("skills_disabled", [])
        if (
            not isinstance(skills_disabled, list)
            or len(skills_disabled) > _MAX_RUNTIME_ITEMS
            or not all(
                _is_bounded_safe_text(name, max_chars=_MAX_RUNTIME_TEXT_CHARS)
                for name in skills_disabled
            )
        ):
            raise ValueError("runtime skills list is invalid")
        approval_rules = runtime.get("approval_rules", [])
        if (
            not isinstance(approval_rules, list)
            or len(approval_rules) > _MAX_RUNTIME_ITEMS
            or not all(isinstance(rule, dict) for rule in approval_rules)
        ):
            raise ValueError("runtime approval rules are invalid")
        approval_strings = (
            "tool_name",
            "tool_source",
            "mcp_server",
            "effect_class",
            "profile",
            "pattern",
            "scope_key",
        )
        allowed_actions = {"allow", "warn", "require_approval", "deny"}
        for rule in approval_rules:
            for key in approval_strings:
                value = rule.get(key)
                if value is not None and not _is_bounded_runtime_text(
                    value, max_chars=_MAX_RUNTIME_TEXT_CHARS
                ):
                    raise ValueError("runtime approval rule is invalid")
            action = rule.get("action")
            if not isinstance(action, str) or action not in allowed_actions:
                raise ValueError("runtime approval action is invalid")
        plan = runtime.get("plan_state", {})
        plan_items = plan.get("items", [])
        if not isinstance(plan_items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("step"), str)
            and isinstance(item.get("active_form"), str)
            and item.get("status") in {"pending", "in_progress", "completed"}
            for item in plan_items
        ):
            raise ValueError("runtime plan state is invalid")
        PlanController.validate_items(plan_items)
        for key in ("revision", "session_generation"):
            value = plan.get(key, 0)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("runtime plan revision is invalid")
        for key in ("owner_agent_id", "explanation", "event_id"):
            value = plan.get(key)
            if value is not None and not _is_bounded_runtime_text(
                value, max_chars=_MAX_RUNTIME_TEXT_CHARS
            ):
                raise ValueError("runtime plan metadata is invalid")
        progress = runtime.get("progress_state", {})
        if progress:
            if progress.get("phase") not in {
                "investigating",
                "implementing",
                "verifying",
                "ready",
                "blocked",
            }:
                raise ValueError("runtime progress phase is invalid")
            revision = progress.get("revision", 0)
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
                or revision > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("runtime progress revision is invalid")
            for key in ("summary", "next", "event_id"):
                value = progress.get(key)
                if value is not None and not _is_bounded_runtime_text(
                    value, max_chars=_MAX_RUNTIME_TEXT_CHARS
                ):
                    raise ValueError("runtime progress metadata is invalid")

    def _validate_canonical_manifest(
        self,
        manifest: dict,
        directory: Path,
        *,
        expected_session_id: str | None,
    ) -> tuple[SessionRestoreIssue, ...]:
        preview_invalid = not is_safe_session_preview(manifest.get("preview", ""))
        if preview_invalid:
            manifest["preview"] = ""
        metadata = SessionMetadata(
            id=manifest.get("id"),
            model=manifest.get("model"),
            saved_at=manifest.get("saved_at"),
            preview=manifest.get("preview", ""),
            fingerprint=manifest.get("fingerprint", DEFAULT_SESSION_FINGERPRINT),
        )
        self._validate_metadata(metadata, ref="manifest")
        self._parse_saved_at(metadata.saved_at, ref="manifest")
        if self._get_session_directory(metadata.id) != directory or (
            expected_session_id is not None and metadata.id != expected_session_id
        ):
            raise SessionRestoreError(
                phase="manifest_validate",
                error_type="SessionIdentityMismatchError",
                ref="manifest",
            ) from None
        if "storage_schema_version" in manifest:
            schema_version = manifest["storage_schema_version"]
            if (
                not isinstance(schema_version, int)
                or isinstance(schema_version, bool)
                or schema_version < 1
            ):
                raise SessionRestoreError(
                    phase="manifest_validate",
                    error_type="SessionStorageSchemaValidationError",
                    ref="manifest",
                ) from None
            if schema_version > _SESSION_STORAGE_SCHEMA_VERSION:
                raise SessionRestoreError(
                    phase="manifest_validate",
                    error_type="UnsupportedSessionStorageSchemaError",
                    ref="manifest",
                ) from None
        if (
            "event_payload_policy" in manifest
            and manifest["event_payload_policy"] != "bounded"
        ):
            raise SessionRestoreError(
                phase="manifest_validate",
                error_type="EventPayloadPolicyValidationError",
                ref="manifest",
            ) from None
        active_mode = manifest.get("active_mode")
        history_completeness = manifest.get("history_completeness")
        if (
            active_mode is not None
            and not _is_bounded_safe_text(
                active_mode, max_chars=_MAX_RUNTIME_TEXT_CHARS
            )
        ) or (
            history_completeness is not None
            and (
                not isinstance(history_completeness, str)
                or history_completeness not in _HISTORY_COMPLETENESS_VALUES
            )
        ):
            raise SessionRestoreError(
                phase="manifest_validate",
                error_type="SessionManifestValidationError",
                ref="manifest",
            ) from None
        if "runtime_state" in manifest:
            try:
                self._validate_runtime_state_payload(manifest["runtime_state"])
            except ValueError:
                raise SessionRestoreError(
                    phase="manifest_validate",
                    error_type="SessionRuntimeStateValidationError",
                    ref="manifest",
                ) from None
        raw_restore_issues = manifest.get("restore_issues", [])
        issue_collector = _collect_persisted_restore_issues(raw_restore_issues)
        if preview_invalid:
            issue_collector.record(
                "preview_validate",
                "SessionPreviewValidationError",
                "preview",
            )
        manifest["restore_issues"] = [
            issue.to_dict() for issue in issue_collector.facts()
        ]
        for key in ("total_prompt_tokens", "total_completion_tokens"):
            value = manifest.get(key, 0)
            if _is_valid_token_count(value):
                continue
            # These are cumulative provider usage counters, so zero is the
            # only honest fallback when their persisted projection is invalid.
            manifest[key] = 0
            issue_collector.record(
                "token_counts_validate",
                "TokenUsageCounterValidationError",
                key,
            )
        if "message_token_counts" in manifest:
            token_counts = manifest["message_token_counts"]
            if not isinstance(token_counts, list) or not all(
                value is None or _is_valid_token_count(value) for value in token_counts
            ):
                manifest.pop("message_token_counts", None)
                issue_collector.record(
                    "token_counts_validate",
                    "MessageTokenCountsValidationError",
                    "message_token_counts",
                )
        return issue_collector.facts()

    def _metadata_rank(
        self,
        metadata: SessionMetadata,
        source_path: Path,
        *,
        ref: str,
    ) -> tuple[int, float, str]:
        try:
            self._safe_path_status(
                source_path,
                phase="inventory_rank",
                ref=ref,
            )
            modified_ns = source_path.stat().st_mtime_ns
        except SessionRestoreError:
            raise
        except OSError as error:
            raise SessionRestoreError(
                phase="inventory_rank",
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None
        saved_at_rank = self._parse_saved_at(metadata.saved_at, ref=ref)
        try:
            saved_timestamp = saved_at_rank.timestamp()
        except (OSError, OverflowError, ValueError) as error:
            raise SessionRestoreError(
                phase="inventory_rank",
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None
        return modified_ns, saved_timestamp, metadata.id

    def _load_directory_metadata(
        self,
        directory: Path,
    ) -> tuple[SessionMetadata, tuple[SessionRestoreIssue, ...], dict]:
        manifest_path = directory / "manifest.json"
        manifest = self._read_json_artifact(manifest_path, ref="manifest")
        issues = self._validate_canonical_manifest(
            manifest,
            directory,
            expected_session_id=directory.name,
        )
        metadata = SessionMetadata(
            id=manifest.get("id"),
            model=manifest.get("model"),
            saved_at=manifest.get("saved_at"),
            preview=manifest.get("preview", ""),
            fingerprint=manifest.get("fingerprint", DEFAULT_SESSION_FINGERPRINT),
        )
        return metadata, issues, manifest

    def _atomic_write_json(self, path: Path, payload: dict, *, ref: str) -> None:
        try:
            _validate_strict_utf8_tree(payload)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, UnicodeError, ValueError) as error:
            raise SessionRestoreError(
                phase=f"{ref}_write_validate",
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None
        self._atomic_replace_bytes(
            path,
            encoded,
            phase=f"{ref}_write",
            ref=ref,
        )

    def _atomic_replace_bytes(
        self,
        path: Path,
        payload: bytes,
        *,
        phase: str,
        ref: str,
    ) -> None:
        self._safe_path_status(path, phase=phase, ref=ref)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        self._safe_path_status(temporary, phase=phase, ref=ref)
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        except OSError as error:
            raise SessionRestoreError(
                phase=phase,
                error_type=_safe_error_type(error),
                ref=ref,
            ) from None

    @classmethod
    def _encode_history_event(cls, event: HistoryEvent) -> bytes:
        event, _ = cls._compact_legacy_request_event(event)
        payload = event.to_dict()
        _validate_strict_utf8_tree(payload)
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
            + b"\n"
        )

    @classmethod
    def _history_event_fingerprint(cls, event: HistoryEvent) -> bytes:
        return hashlib.sha256(cls._encode_history_event(event)).digest()

    def _atomic_replace_history(
        self,
        path: Path,
        events: list[HistoryEvent],
    ) -> None:
        """Stream a complete ledger into an inert temp file, then replace it."""
        self._safe_path_status(path, phase="history_write", ref="history_ledger")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        self._safe_path_status(
            temporary,
            phase="history_write",
            ref="history_ledger",
        )
        try:
            with temporary.open("xb") as stream:
                for event in events:
                    stream.write(self._encode_history_event(event))
            temporary.replace(path)
        except (TypeError, UnicodeError, ValueError) as error:
            # A failed temp file is not referenced by the manifest and is inert.
            raise SessionRestoreError(
                phase="history_write_validate",
                error_type=_safe_error_type(error),
                ref="history_ledger",
            ) from None
        except OSError as error:
            raise SessionRestoreError(
                phase="history_write",
                error_type=_safe_error_type(error),
                ref="history_ledger",
            ) from None

    def _write_session_directory(
        self,
        session: Session,
        *,
        incremental: bool = False,
        events_already_persisted: bool = False,
        additional_events: tuple[HistoryEvent, ...] = (),
    ) -> None:
        directory = self._get_session_directory(session.id)
        self._ensure_safe_directory(directory, ref="session_directory")
        requests_dir = directory / "requests"
        self._ensure_safe_directory(requests_dir, ref="request_record")
        checkpoints_dir = directory / "checkpoints"
        self._ensure_safe_directory(checkpoints_dir, ref="checkpoint")

        for request in session.request_envelopes:
            if not _is_safe_artifact_id(request.request_id):
                raise SessionRestoreError(
                    phase="request_record_validate",
                    error_type="ArtifactIdentityValidationError",
                    ref="request_record",
                ) from None
        for checkpoint in session.checkpoints:
            if not _is_safe_artifact_id(checkpoint.id):
                raise SessionRestoreError(
                    phase="checkpoint_validate",
                    error_type="ArtifactIdentityValidationError",
                    ref="checkpoint",
                ) from None

        cursor = self._write_cursors.setdefault(session.id, _DirectoryWriteCursor())
        if incremental and not cursor.initialized:
            # The manifest projection is authoritative. Files not referenced by
            # the in-memory session are inert and belong to a separate GC path.
            # Rewrite the bounded live projection once instead of scanning an
            # unbounded directory of obsolete artifacts.
            cursor.request_ids.clear()
            cursor.checkpoint_ids.clear()
            cursor.initialized = True

        events_path = directory / "events.jsonl"
        self._safe_path_status(
            events_path,
            phase="history_write",
            ref="history_ledger",
        )
        new_events: list[HistoryEvent] = list(additional_events)
        replace_history = not incremental and not events_already_persisted
        if not events_already_persisted:
            existing_event_fingerprints: dict[str, bytes] = {}
            existing_last_seq = 0
            if events_path.exists():
                try:
                    previous_seq = 0
                    with events_path.open("rb") as stream:
                        for line in stream:
                            decoded = json.loads(
                                line,
                                parse_constant=_reject_non_finite_json,
                            )
                            _validate_strict_utf8_tree(decoded)
                            self._validate_history_event_payload(
                                decoded,
                                expected_session_id=session.id,
                            )
                            event = HistoryEvent.from_dict(decoded)
                            if (
                                event.event_id in existing_event_fingerprints
                                or event.seq <= previous_seq
                            ):
                                raise ValueError("history event ordering is invalid")
                            compacted, _ = self._compact_legacy_request_event(event)
                            existing_event_fingerprints[event.event_id] = (
                                self._history_event_fingerprint(compacted)
                            )
                            previous_seq = event.seq
                    existing_last_seq = previous_seq
                except (
                    AttributeError,
                    json.JSONDecodeError,
                    KeyError,
                    OSError,
                    OverflowError,
                    RecursionError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                ) as error:
                    raise SessionRestoreError(
                        phase="history_write_validate",
                        error_type=_safe_error_type(error),
                        ref="history_ledger",
                    ) from None
            desired_events: list[HistoryEvent] = []
            desired_ids: set[str] = set()
            desired_previous_seq = 0
            for event in sorted(session.history_events, key=lambda item: item.seq):
                compacted, _ = self._compact_legacy_request_event(event)
                payload = compacted.to_dict()
                try:
                    _validate_strict_utf8_tree(payload)
                    self._validate_history_event_payload(
                        payload,
                        expected_session_id=session.id,
                    )
                    if (
                        compacted.event_id in desired_ids
                        or compacted.seq <= desired_previous_seq
                    ):
                        raise ValueError("history event ordering is invalid")
                except (TypeError, UnicodeError, ValueError) as error:
                    raise SessionRestoreError(
                        phase="history_write_validate",
                        error_type=_safe_error_type(error),
                        ref="history_ledger",
                    ) from None
                desired_ids.add(compacted.event_id)
                desired_previous_seq = compacted.seq
                desired_events.append(compacted)
                existing_fingerprint = existing_event_fingerprints.get(
                    compacted.event_id
                )
                if existing_fingerprint is not None:
                    if existing_fingerprint != self._history_event_fingerprint(
                        compacted
                    ):
                        raise SessionRestoreError(
                            phase="history_write_validate",
                            error_type="HistoryEventConflictError",
                            ref="history_ledger",
                        ) from None
            if replace_history:
                new_events = desired_events
            else:
                new_events = [
                    event
                    for event in desired_events
                    if event.event_id not in existing_event_fingerprints
                ]
                if new_events and new_events[0].seq <= existing_last_seq:
                    raise SessionRestoreError(
                        phase="history_write_validate",
                        error_type="HistorySequenceConflictError",
                        ref="history_ledger",
                    ) from None
        if new_events or replace_history:
            action = "Writing" if replace_history else "Appending"
            self._report_progress(f"{action} {len(new_events)} history event(s)...")
            if replace_history:
                self._atomic_replace_history(events_path, new_events)
            else:
                try:
                    encoded_events = [
                        self._encode_history_event(event) for event in new_events
                    ]
                    with events_path.open("ab") as stream:
                        for encoded_event in encoded_events:
                            stream.write(encoded_event)
                except (TypeError, UnicodeError, ValueError) as error:
                    raise SessionRestoreError(
                        phase="history_write_validate",
                        error_type=_safe_error_type(error),
                        ref="history_ledger",
                    ) from None
                except OSError as error:
                    raise SessionRestoreError(
                        phase="history_write",
                        error_type=_safe_error_type(error),
                        ref="history_ledger",
                    ) from None

        replay = session.replay_envelope
        if replay is not None:
            self._report_progress(
                f"Writing replay snapshot ({len(replay.items)} item(s))..."
            )
            self._atomic_write_json(
                directory / "replay.json", replay.to_dict(), ref="replay"
            )
        pending_requests = [
            request
            for request in session.request_envelopes
            if not incremental or request.request_id not in cursor.request_ids
        ]
        if pending_requests:
            self._report_progress(
                f"Writing {len(pending_requests)} request record(s)..."
            )
        for request in pending_requests:
            self._atomic_write_json(
                requests_dir / f"{request.request_id}.json",
                request.to_dict(),
                ref="request_record",
            )
            cursor.request_ids.add(request.request_id)
        pending_checkpoints = [
            checkpoint
            for checkpoint in session.checkpoints
            if not incremental or checkpoint.id not in cursor.checkpoint_ids
        ]
        if pending_checkpoints:
            self._report_progress(
                f"Writing {len(pending_checkpoints)} context checkpoint(s)..."
            )
        for checkpoint in pending_checkpoints:
            self._atomic_write_json(
                checkpoints_dir / f"{checkpoint.id}.json",
                checkpoint.to_dict(),
                ref="checkpoint",
            )
            cursor.checkpoint_ids.add(checkpoint.id)
        manifest = session.to_dict()
        manifest.pop("messages", None)
        manifest.pop("history_events", None)
        manifest.pop("replay_envelope", None)
        manifest.pop("request_envelopes", None)
        manifest.pop("checkpoints", None)
        manifest["checkpoint_ids"] = [item.id for item in session.checkpoints]
        manifest["request_ids"] = [
            item.request_id for item in session.request_envelopes
        ]
        manifest["preview"] = session.get_preview()
        manifest["message_token_counts"] = [
            message.get(MESSAGE_TOKEN_KEY) for message in session.messages
        ]
        manifest["storage_schema_version"] = _SESSION_STORAGE_SCHEMA_VERSION
        manifest["event_payload_policy"] = "bounded"
        manifest["event_count"] = len(session.history_events)
        manifest["last_event_seq"] = max(
            max((event.seq for event in session.history_events), default=0),
            session.history_next_seq_floor,
        )
        self._report_progress("Committing session manifest...")
        self._atomic_write_json(directory / "manifest.json", manifest, ref="manifest")
        cursor.initialized = True
        cursor.request_ids.intersection_update(manifest["request_ids"])
        cursor.checkpoint_ids.intersection_update(manifest["checkpoint_ids"])

    def _load_session_directory(
        self,
        directory: Path,
        *,
        expected_session_id: str | None = None,
    ) -> Session:
        directory_status = self._safe_path_status(
            directory,
            phase="session_discovery",
            ref="session_directory",
        )
        if directory_status is None or not stat.S_ISDIR(directory_status.st_mode):
            raise SessionRestoreError(
                phase="session_discovery",
                error_type=(
                    "FileNotFoundError"
                    if directory_status is None
                    else "NotADirectoryError"
                ),
                ref="session_directory",
            ) from None
        manifest_path = directory / "manifest.json"
        replay_path = directory / "replay.json"
        self._report_progress("Reading session manifest and replay snapshot...")
        manifest = self._read_json_artifact(manifest_path, ref="manifest")
        persisted_issues = self._validate_canonical_manifest(
            manifest,
            directory,
            expected_session_id=expected_session_id,
        )
        replay_payload = self._read_json_artifact(replay_path, ref="replay")
        try:
            validate_replay_payload(
                replay_payload,
                expected_session_id=manifest["id"],
            )
            replay = ReplayEnvelope.from_dict(replay_payload)
        except Exception as error:
            replay_error_type = _safe_error_type(error)
        else:
            replay_error_type = None
        if replay_error_type is not None:
            raise SessionRestoreError(
                phase="replay_validate",
                error_type=replay_error_type,
                ref="replay",
            ) from None
        try:
            replay_valid = replay.validate() and replay.validate_protocol()
        except Exception as error:
            replay_error_type = _safe_error_type(error)
            replay_valid = False
        if not replay_valid:
            raise SessionRestoreError(
                phase="replay_validate",
                error_type=replay_error_type or "ReplayValidationError",
                ref="replay",
            ) from None

        events_path = directory / "events.jsonl"
        expected_event_count = manifest.get("event_count")
        expected_last_event_seq = manifest.get("last_event_seq")
        trusted_last_event_seq = (
            expected_last_event_seq
            if isinstance(expected_last_event_seq, int)
            and not isinstance(expected_last_event_seq, bool)
            and expected_last_event_seq >= 0
            and expected_last_event_seq <= _MAX_PERSISTED_COUNTER
            else 0
        )
        history_result = self._load_history_events(
            events_path,
            expected_session_id=manifest["id"],
            trusted_last_event_seq=trusted_last_event_seq,
        )
        events = list(history_result.events)
        history_issues = history_result.issues
        if expected_event_count == 0:
            history_issues = tuple(
                issue
                for issue in history_issues
                if not (
                    issue.phase == "history_read"
                    and issue.error_type == "FileNotFoundError"
                )
            )
        issue_collector = _RestoreIssueCollector(persisted_issues)
        for issue in history_issues:
            issue_collector.add(issue)
        history_restore_degraded = any(
            issue.ref == "history_ledger"
            for issue in (*persisted_issues, *history_issues)
        )

        def record_restore_issue(
            phase: str,
            error_type: str,
            ref: str,
        ) -> None:
            issue_collector.record(phase, error_type, ref)

        def referenced_paths(
            directory_path: Path,
            manifest_key: str,
            artifact_ref: str,
            *,
            max_records: int | None = None,
            newest_by_mtime: bool = False,
        ) -> list[Path]:
            try:
                directory_status = self._safe_path_status(
                    directory_path,
                    phase=f"{artifact_ref}_read",
                    ref=artifact_ref,
                )
            except SessionRestoreError as error:
                record_restore_issue(error.phase, error.error_type, error.ref)
                return []
            if directory_status is not None and not stat.S_ISDIR(
                directory_status.st_mode
            ):
                record_restore_issue(
                    f"{artifact_ref}_read",
                    "NotADirectoryError",
                    artifact_ref,
                )
                return []
            record_ids = manifest.get(manifest_key)
            if isinstance(record_ids, list):
                paths: list[Path] = []
                selected_ids = record_ids[-max_records:] if max_records else record_ids
                for record_id in selected_ids:
                    if not _is_safe_artifact_id(record_id):
                        record_restore_issue(
                            f"{artifact_ref}_validate",
                            "ArtifactReferenceValidationError",
                            artifact_ref,
                        )
                        continue
                    paths.append(directory_path / f"{record_id}.json")
                return paths
            if manifest_key in manifest:
                record_restore_issue(
                    f"{artifact_ref}_validate",
                    "ArtifactReferenceValidationError",
                    artifact_ref,
                )
            try:
                paths = list(directory_path.glob("*.json"))
                for path in paths:
                    self._safe_path_status(
                        path,
                        phase=f"{artifact_ref}_read",
                        ref=artifact_ref,
                    )
                    if not _is_safe_artifact_id(path.stem):
                        raise SessionRestoreError(
                            phase=f"{artifact_ref}_validate",
                            error_type="ArtifactIdentityValidationError",
                            ref=artifact_ref,
                        ) from None
                paths.sort(
                    key=(
                        (lambda path: path.stat().st_mtime_ns)
                        if newest_by_mtime
                        else (lambda path: path.name)
                    )
                )
            except SessionRestoreError as error:
                record_restore_issue(error.phase, error.error_type, error.ref)
                return []
            except OSError as error:
                record_restore_issue(
                    f"{artifact_ref}_read",
                    _safe_error_type(error),
                    artifact_ref,
                )
                return []
            return paths[-max_records:] if max_records else paths

        if "event_count" in manifest and (
            not isinstance(expected_event_count, int)
            or isinstance(expected_event_count, bool)
            or expected_event_count < 0
            or expected_event_count > _MAX_PERSISTED_COUNTER
        ):
            record_restore_issue(
                "history_validate",
                "HistoryEventCountValidationError",
                "history_ledger",
            )
            history_restore_degraded = True
        elif isinstance(expected_event_count, int) and expected_event_count > len(
            events
        ):
            record_restore_issue(
                "history_validate",
                "HistoryEventCountMismatch",
                "history_ledger",
            )
            history_restore_degraded = True
        if "last_event_seq" in manifest:
            if (
                not isinstance(expected_last_event_seq, int)
                or isinstance(expected_last_event_seq, bool)
                or expected_last_event_seq < 0
                or expected_last_event_seq > _MAX_PERSISTED_COUNTER
            ):
                record_restore_issue(
                    "history_validate",
                    "HistoryLastEventSequenceValidationError",
                    "history_ledger",
                )
                history_restore_degraded = True
            elif expected_last_event_seq > max(
                (event.seq for event in events), default=0
            ):
                record_restore_issue(
                    "history_validate",
                    "HistoryLastEventSequenceMismatch",
                    "history_ledger",
                )
                history_restore_degraded = True
        requests: list[RequestEnvelope] = []
        requests_dir = directory / "requests"
        request_paths = referenced_paths(
            requests_dir,
            "request_ids",
            "request_record",
            max_records=_MAX_REQUEST_RECORDS,
            newest_by_mtime=True,
        )
        if request_paths:
            self._report_progress(f"Reading {len(request_paths)} request record(s)...")
        for request_path in request_paths:
            try:
                request_payload = self._read_json_artifact(
                    request_path,
                    ref="request_record",
                )
            except SessionRestoreError as error:
                record_restore_issue(error.phase, error.error_type, error.ref)
                continue
            try:
                self._validate_request_record_payload(
                    request_payload,
                    expected_id=request_path.stem,
                )
                requests.append(RequestEnvelope(**request_payload))
            except Exception as error:
                record_restore_issue(
                    "request_record_validate",
                    _safe_error_type(error),
                    "request_record",
                )
        checkpoints: list[CompactionCheckpoint] = []
        checkpoints_dir = directory / "checkpoints"
        checkpoint_paths = referenced_paths(
            checkpoints_dir,
            "checkpoint_ids",
            "checkpoint",
        )
        if checkpoint_paths:
            self._report_progress(
                f"Reading {len(checkpoint_paths)} context checkpoint(s)..."
            )
        for checkpoint_path in checkpoint_paths:
            try:
                checkpoint_payload = self._read_json_artifact(
                    checkpoint_path,
                    ref="checkpoint",
                )
            except SessionRestoreError as error:
                record_restore_issue(error.phase, error.error_type, error.ref)
                continue
            try:
                self._validate_checkpoint_payload(
                    checkpoint_payload,
                    expected_id=checkpoint_path.stem,
                )
                checkpoints.append(CompactionCheckpoint.from_dict(checkpoint_payload))
            except Exception as error:
                record_restore_issue(
                    "checkpoint_validate",
                    _safe_error_type(error),
                    "checkpoint",
                )
        # Avoid serializing these potentially huge structures to dictionaries
        # only for Session.from_dict() to reconstruct the same objects again.
        for key in (
            "messages",
            "history_events",
            "replay_envelope",
            "request_envelopes",
            "checkpoints",
        ):
            manifest.pop(key, None)
        try:
            session = Session.from_dict(manifest)
        except Exception as error:
            manifest_error_type = _safe_error_type(error)
        else:
            manifest_error_type = None
        if manifest_error_type is not None:
            raise SessionRestoreError(
                phase="manifest_validate",
                error_type=manifest_error_type,
                ref="manifest",
            ) from None
        session.messages = []
        for replay_item in replay.items:
            message = dict(replay_item)
            if MESSAGE_TOKEN_KEY in message and not _is_valid_token_count(
                message[MESSAGE_TOKEN_KEY]
            ):
                record_restore_issue(
                    "token_counts_validate",
                    "MessageTokenMetadataValidationError",
                    "message_token_metadata",
                )
            # The compact manifest is the canonical cache projection. Never
            # let duplicated metadata embedded in replay bypass validation.
            message.pop(MESSAGE_TOKEN_KEY, None)
            session.messages.append(message)
        token_counts = manifest.get("message_token_counts", [])
        if "message_token_counts" in manifest and len(token_counts) != len(
            session.messages
        ):
            record_restore_issue(
                "token_counts_validate",
                "MessageTokenCountMismatch",
                "message_token_counts",
            )
        elif len(token_counts) == len(session.messages):
            for message, token_count in zip(session.messages, token_counts):
                if isinstance(token_count, int):
                    message[MESSAGE_TOKEN_KEY] = token_count
        recovered_count = 0
        if not history_restore_degraded:
            session.messages, recovered_count = self._recover_replay_tail(
                session.messages,
                replay,
                events,
            )
        for message in session.messages:
            if MESSAGE_TOKEN_KEY not in message or _is_valid_token_count(
                message[MESSAGE_TOKEN_KEY]
            ):
                continue
            message.pop(MESSAGE_TOKEN_KEY, None)
            record_restore_issue(
                "token_counts_validate",
                "MessageTokenMetadataValidationError",
                "message_token_metadata",
            )
        if recovered_count:
            self._report_progress(
                f"Recovering {recovered_count} message update(s) from "
                "the durable history tail..."
            )
            self._ensure_message_token_counts(
                session.messages,
                reason="recovered",
            )
            self._report_progress(
                f"Recovered {recovered_count} message update(s) from "
                "the durable history tail."
            )
        session.replay_envelope = replay
        session.history_events = events
        session.history_next_seq_floor = history_result.next_seq_floor
        session.history_behavior_projection_safe = (
            not history_issues
            and not history_restore_degraded
            and session.history_completeness == "complete"
        )
        session.request_envelopes = requests
        session.checkpoints = checkpoints
        session.restore_issues = issue_collector.facts()
        if history_restore_degraded:
            session.history_completeness = "degraded"
        return session

    @staticmethod
    def _validate_request_record_payload(
        payload: dict,
        *,
        expected_id: str,
    ) -> None:
        request_id = payload.get("request_id")
        if (
            request_id != expected_id
            or not _is_safe_artifact_id(request_id)
            or not _is_safe_artifact_id(expected_id)
        ):
            raise ValueError("request record identity is invalid")
        schema_version = payload.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version < 1
            or schema_version > 1
        ):
            raise ValueError("request record version is invalid")
        for key in (
            "execution_overlay_revision",
            "execution_overlay_tokens",
            "plan_revision",
        ):
            value = payload.get(key, 0 if key == "plan_revision" else None)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("request record counter is invalid")
        for key in (
            "replay_envelope_hash",
            "execution_overlay_hash",
            "canonical_request_hash",
        ):
            value = payload.get(key)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise ValueError("request record hash is invalid")

    @staticmethod
    def _validate_checkpoint_payload(
        payload: dict,
        *,
        expected_id: str,
    ) -> None:
        if (
            payload.get("id") != expected_id
            or not _is_safe_artifact_id(payload.get("id"))
            or not _is_safe_artifact_id(expected_id)
        ):
            raise ValueError("checkpoint identity is invalid")
        trigger = payload.get("trigger")
        strategy = payload.get("strategy")
        replacement_history = payload.get("replacement_history")
        if (
            not isinstance(trigger, str)
            or not trigger
            or not isinstance(strategy, (list, tuple))
            or not all(isinstance(item, str) and item for item in strategy)
            or not isinstance(replacement_history, (list, tuple))
            or not all(isinstance(item, dict) for item in replacement_history)
        ):
            raise ValueError("checkpoint content is invalid")
        for message in replacement_history:
            validate_provider_message(message)
        for key in (
            "source_history_version",
            "tokens_before",
            "tokens_after",
            "preserved_rounds",
            "cache_epoch",
        ):
            value = payload.get(key, 0)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("checkpoint counter is invalid")
        for key in (
            "actual_prompt_tokens",
            "cached_input_tokens",
            "invalidated_suffix_tokens",
            "reclaimed_tokens",
        ):
            value = payload.get(key)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("checkpoint optional counter is invalid")
        created_at = payload.get("created_at")
        if (
            not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not math.isfinite(created_at)
            or created_at < 0
        ):
            raise ValueError("checkpoint timestamp is invalid")

    @staticmethod
    def _recover_replay_tail(
        snapshot_messages: list[dict],
        replay: ReplayEnvelope,
        events: list[HistoryEvent],
    ) -> tuple[list[dict], int]:
        """Apply durable message/view events committed after the replay snapshot."""
        if not events:
            return snapshot_messages, 0
        seq_by_id = {event.event_id: event.seq for event in events}
        snapshot_seq = max(
            (
                seq_by_id.get(str(event_id), 0)
                for provenance in replay.item_provenance
                for event_id in provenance.get("source_event_ids", ())
            ),
            default=0,
        )
        if snapshot_seq <= 0 and snapshot_messages:
            # Legacy replay provenance cannot safely establish a boundary.
            # Its synchronous snapshots remain authoritative.
            return snapshot_messages, 0

        recovered = [dict(message) for message in snapshot_messages]
        applied = 0
        for event in events:
            if event.seq <= snapshot_seq:
                continue
            if event.kind == "context_view_committed":
                items = event.payload.get("items")
                if isinstance(items, list) and all(
                    isinstance(item, dict) for item in items
                ):
                    recovered = [dict(item) for item in items]
                    applied += 1
                continue
            if event.kind != "message_committed":
                continue
            message = event.payload.get("message")
            if isinstance(message, dict):
                recovered.append(dict(message))
                applied += 1
        return recovered, applied

    def _load_history_events(
        self,
        events_path: Path,
        *,
        expected_session_id: str,
        trusted_last_event_seq: int = 0,
    ) -> _HistoryLoadResult:
        """Load history, retaining safe facts for every recoverable failure."""
        events: list[HistoryEvent] = []
        issues = _RestoreIssueCollector()
        seen_event_ids: set[str] = set()
        previous_seq = 0
        physical_line_count = 0
        decoded_sequence_floor = 0

        def record_issue(phase: str, error_type: str) -> None:
            issues.record(phase, error_type, "history_ledger")

        try:
            events_status = self._safe_path_status(
                events_path,
                phase="history_read",
                ref="history_ledger",
            )
        except SessionRestoreError as error:
            record_issue(error.phase, error.error_type)
            return _HistoryLoadResult(
                (), issues.facts(), max(0, trusted_last_event_seq)
            )
        if events_status is None:
            record_issue("history_read", "FileNotFoundError")
            return _HistoryLoadResult(
                (), issues.facts(), max(0, trusted_last_event_seq)
            )
        if not stat.S_ISREG(events_status.st_mode):
            record_issue("history_read", "NotAFileError")
            return _HistoryLoadResult(
                (), issues.facts(), max(0, trusted_last_event_seq)
            )
        total_bytes = events_status.st_size

        total_mb = total_bytes / (1024 * 1024)
        self._report_progress(f"Reading history ledger ({total_mb:.1f} MB)...")
        started = time.monotonic()
        next_percent = 10
        try:
            with events_path.open("rb") as stream:
                for line in stream:
                    physical_line_count += 1
                    failure_type: str | None = None
                    try:
                        payload = json.loads(
                            line,
                            parse_constant=_reject_non_finite_json,
                        )
                        _validate_strict_utf8_tree(payload)
                        if isinstance(payload, dict):
                            raw_seq = payload.get("seq")
                            if (
                                isinstance(raw_seq, int)
                                and not isinstance(raw_seq, bool)
                                and raw_seq >= 0
                                and raw_seq <= _MAX_PERSISTED_COUNTER
                            ):
                                decoded_sequence_floor = max(
                                    decoded_sequence_floor, raw_seq
                                )
                        self._validate_history_event_payload(
                            payload,
                            expected_session_id=expected_session_id,
                        )
                        event = HistoryEvent.from_dict(payload)
                        if (
                            event.event_id in seen_event_ids
                            or event.seq <= previous_seq
                        ):
                            raise ValueError("history event ordering is invalid")
                        event, _ = self._compact_legacy_request_event(event)
                    except (
                        AttributeError,
                        json.JSONDecodeError,
                        KeyError,
                        OverflowError,
                        RecursionError,
                        TypeError,
                        UnicodeError,
                        ValueError,
                    ) as error:
                        failure_type = _safe_error_type(error)
                    if failure_type is not None:
                        record_issue("history_decode", failure_type)
                        continue
                    events.append(event)
                    seen_event_ids.add(event.event_id)
                    previous_seq = event.seq
                    if total_bytes and time.monotonic() - started >= 0.5:
                        percent = min(100, int(stream.tell() * 100 / total_bytes))
                        if percent >= next_percent and percent < 100:
                            self._report_progress(
                                f"Reading history ledger... {percent}% "
                                f"({len(events)} event(s))."
                            )
                            next_percent = (percent // 10 + 1) * 10
        except (OSError, UnicodeError) as error:
            record_issue("history_read", _safe_error_type(error))

        self._report_progress(
            f"History ledger ready ({len(events)} event(s), "
            f"{time.monotonic() - started:.1f}s)."
        )
        return _HistoryLoadResult(
            tuple(events),
            issues.facts(),
            max(
                trusted_last_event_seq,
                decoded_sequence_floor,
                physical_line_count,
                max((event.seq for event in events), default=0),
            ),
        )

    @staticmethod
    def _validate_history_event_payload(
        payload: object, *, expected_session_id: str | None = None
    ) -> None:
        if not isinstance(payload, dict):
            raise TypeError("history event must be an object")
        event_id = payload.get("event_id")
        kind = payload.get("kind")
        seq = payload.get("seq")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 64
            or re.fullmatch(r"[A-Za-z0-9_.-]+", event_id) is None
            or not isinstance(kind, str)
            or not kind
            or len(kind) > 64
            or re.fullmatch(r"[A-Za-z0-9_.-]+", kind) is None
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq <= 0
            or seq > _MAX_PERSISTED_COUNTER
            or not isinstance(payload.get("payload"), dict)
        ):
            raise ValueError("history event identity is invalid")
        for key in ("schema_version", "session_generation"):
            value = payload.get(key, 1 if key == "schema_version" else 0)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < (1 if key == "schema_version" else 0)
                or (key == "schema_version" and value > 2)
                or (key == "session_generation" and value > _MAX_PERSISTED_COUNTER)
            ):
                raise ValueError("history event version is invalid")
        timestamp = payload.get("timestamp", payload.get("created_at", 0.0))
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise ValueError("history event timestamp is invalid")
        session_id = payload.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str)
            or not session_id
            or (expected_session_id is not None and session_id != expected_session_id)
        ):
            raise ValueError("history event session identity is invalid")
        for key in (
            "agent_id",
            "parent_agent_id",
            "job_id",
            "turn_id",
            "api_round_id",
            "role",
        ):
            value = payload.get(key)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 256
            ):
                raise ValueError("history event optional text is invalid")
        for key in ("artifact_refs", "supersedes_event_ids"):
            refs = payload.get(key, ())
            if not isinstance(refs, (list, tuple)) or not all(
                isinstance(ref, str) and ref for ref in refs
            ):
                raise ValueError("history event references are invalid")
        event_payload = payload["payload"]
        if kind == "message_committed":
            source = event_payload.get("source")
            if not isinstance(source, str) or not source:
                raise ValueError("history message source is invalid")
            validate_provider_message(event_payload.get("message"))
            for key in ("steering_id", "attempt_id"):
                value = event_payload.get(key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError("history message control metadata is invalid")
        elif kind == "context_view_committed":
            reason = event_payload.get("reason")
            history_version = event_payload.get("history_version")
            checkpoint_id = event_payload.get("checkpoint_id")
            items = event_payload.get("items")
            if (
                not isinstance(reason, str)
                or not reason
                or not isinstance(history_version, int)
                or isinstance(history_version, bool)
                or history_version < 0
                or (
                    checkpoint_id is not None
                    and (not isinstance(checkpoint_id, str) or not checkpoint_id)
                )
                or not isinstance(items, list)
            ):
                raise ValueError("history context view is invalid")
            for item in items:
                validate_provider_message(item)
        elif kind == "usage_observed":
            for key in (
                "actual_prompt_tokens",
                "local_request_estimate",
                "local_history_estimate",
            ):
                value = event_payload.get(key)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > _MAX_PERSISTED_COUNTER
                ):
                    raise ValueError("history usage counter is invalid")
            cached_input_tokens = event_payload.get("cached_input_tokens")
            if cached_input_tokens is not None and (
                not isinstance(cached_input_tokens, int)
                or isinstance(cached_input_tokens, bool)
                or cached_input_tokens < 0
                or cached_input_tokens > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("history cached usage is invalid")
            for key in ("request_boundary", "model_profile"):
                value = event_payload.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError("history usage identity is invalid")
        elif kind == "steering_admitted":
            for key in ("steering_id", "turn_id", "content"):
                value = event_payload.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError("history steering admission is invalid")
            generation = event_payload.get("generation")
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 0
                or generation > _MAX_PERSISTED_COUNTER
                or generation != payload.get("session_generation")
                or (
                    payload.get("turn_id") is not None
                    and payload.get("turn_id") != event_payload["turn_id"]
                )
            ):
                raise ValueError("history steering generation is invalid")
        elif kind in {"steering_applied", "steering_discarded"}:
            steering_id = event_payload.get("steering_id")
            if not isinstance(steering_id, str) or not steering_id:
                raise ValueError("history steering identity is invalid")
            if kind == "steering_applied":
                attempt_id = event_payload.get("attempt_id")
                if attempt_id is not None and (
                    not isinstance(attempt_id, str) or not attempt_id
                ):
                    raise ValueError("history steering attempt is invalid")
            else:
                reason = event_payload.get("reason")
                if not isinstance(reason, str) or not reason:
                    raise ValueError("history steering discard reason is invalid")
        elif kind in {"approval_requested", "approval_resolved"}:
            request_id = event_payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("history approval identity is invalid")
            if kind == "approval_requested":
                tool_name = event_payload.get("tool_name")
                if not isinstance(tool_name, str) or not tool_name:
                    raise ValueError("history approval tool is invalid")
            else:
                approved = event_payload.get("approved")
                if not isinstance(approved, bool):
                    raise ValueError("history approval decision is invalid")
        elif kind == "context_checkpoint":
            checkpoint_id = event_payload.get("checkpoint_id")
            view_event_id = event_payload.get("context_view_event_id")
            history_version = event_payload.get("history_version")
            if (
                not isinstance(checkpoint_id, str)
                or not checkpoint_id
                or not isinstance(view_event_id, str)
                or not view_event_id
                or not isinstance(history_version, int)
                or isinstance(history_version, bool)
                or history_version < 0
                or history_version > _MAX_PERSISTED_COUNTER
            ):
                raise ValueError("history checkpoint is invalid")
        elif kind == "plan_updated":
            owner = event_payload.get("owner_agent_id")
            tool_call_id = event_payload.get("tool_call_id")
            revision = event_payload.get("revision")
            generation = event_payload.get("session_generation")
            explanation = event_payload.get("explanation")
            if (
                not isinstance(owner, str)
                or not owner
                or not isinstance(tool_call_id, str)
                or not tool_call_id
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
                or revision > _MAX_PERSISTED_COUNTER
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation != payload.get("session_generation")
                or (
                    explanation is not None
                    and not _is_bounded_runtime_text(
                        explanation, max_chars=_MAX_RUNTIME_TEXT_CHARS
                    )
                )
            ):
                raise ValueError("history plan metadata is invalid")
            PlanController.validate_items(event_payload.get("items"))
        elif kind == "progress_reported":
            owner = event_payload.get("owner_agent_id")
            tool_call_id = event_payload.get("tool_call_id")
            revision = event_payload.get("revision")
            generation = event_payload.get("session_generation")
            phase = event_payload.get("phase")
            summary = event_payload.get("summary")
            next_step = event_payload.get("next")
            if (
                not isinstance(owner, str)
                or not owner
                or not isinstance(tool_call_id, str)
                or not tool_call_id
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
                or revision > _MAX_PERSISTED_COUNTER
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation != payload.get("session_generation")
                or phase
                not in {
                    "investigating",
                    "implementing",
                    "verifying",
                    "ready",
                    "blocked",
                }
                or not isinstance(summary, str)
                or not summary
                or len(summary) > 500
                or (
                    next_step is not None
                    and (not isinstance(next_step, str) or len(next_step) > 500)
                )
            ):
                raise ValueError("history progress state is invalid")
        elif kind == "subagent_job_changed":
            for key in ("job_id", "mode", "task", "status"):
                value = event_payload.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError("subagent job text is invalid")
            if event_payload["mode"] not in {"explore", "execute", "verify"} or (
                event_payload["status"] not in _SUBAGENT_JOB_STATUSES
            ):
                raise ValueError("subagent job state is invalid")
            if payload.get("job_id") not in (None, event_payload["job_id"]):
                raise ValueError("subagent job attribution is invalid")
            for key in (
                "generation",
                "depth",
                "prompt_tokens",
                "completion_tokens",
                "tool_calls",
                "worker_generation",
                "model_calls",
                "cancellation_epoch",
                "max_rounds",
            ):
                value = event_payload.get(key, 0)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > _MAX_PERSISTED_COUNTER
                ):
                    raise ValueError("subagent job counter is invalid")
            if event_payload.get("generation") != payload.get("session_generation"):
                raise ValueError("subagent job generation is invalid")
            for key in (
                "created_at",
                "started_at",
                "finished_at",
                "guidance_deadline_at",
                "last_activity_at",
                "active_seconds",
            ):
                value = event_payload.get(key)
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("subagent job timestamp is invalid")
            if event_payload.get("created_at") is None:
                raise ValueError("subagent job created_at is missing")
            timeout_seconds = event_payload.get("timeout_seconds")
            if timeout_seconds is not None and (
                not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or timeout_seconds < 0
            ):
                raise ValueError("subagent timeout is invalid")
            for key in (
                "parent_session_id",
                "parent_job_id",
                "context_mode",
                "worktree_path",
                "verification_job_id",
                "verification_for",
                "working_directory",
                "guidance_request_id",
                "resume_reference",
                "current_tool",
                "model_profile_name",
                "agent_id",
                "cancellation_id",
                "result",
                "error",
            ):
                value = event_payload.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError("subagent optional text is invalid")
            progress = event_payload.get("progress", [])
            if not isinstance(progress, list) or not all(
                isinstance(item, str) for item in progress
            ):
                raise ValueError("subagent progress is invalid")
            for key in ("auto_verify", "usage_uncertain", "resume_ready"):
                value = event_payload.get(key, False)
                if not isinstance(value, bool):
                    raise ValueError("subagent boolean state is invalid")
        elif kind in {
            "subagent_communication_queued",
            "subagent_communication_delivered",
        }:
            item_id = event_payload.get("item_id")
            direction = event_payload.get("direction")
            if (
                not isinstance(item_id, str)
                or not item_id
                or direction not in {"child_to_parent", "parent_to_child"}
            ):
                raise ValueError("subagent communication identity is invalid")
            for key in ("generation", "seq"):
                value = event_payload.get(key)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > _MAX_PERSISTED_COUNTER
                ):
                    raise ValueError("subagent communication counter is invalid")
            if event_payload.get("generation") != payload.get("session_generation"):
                raise ValueError("subagent communication generation is invalid")
            content = event_payload.get("content")
            if not isinstance(content, str) or not content:
                raise ValueError("subagent communication content is invalid")
            content_hash = event_payload.get("content_hash")
            if content_hash is not None and (
                not isinstance(content_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
            ):
                raise ValueError("subagent communication hash is invalid")
            if direction == "child_to_parent":
                for key in ("sender_agent_id", "recipient_agent_id"):
                    value = event_payload.get(key)
                    if not isinstance(value, str) or not value:
                        raise ValueError("subagent communication peer is invalid")
                created_at = event_payload.get("created_at")
                if (
                    not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(created_at)
                    or created_at < 0
                    or event_payload.get("kind") not in _SUBAGENT_MESSAGE_KINDS
                ):
                    raise ValueError("subagent communication metadata is invalid")
            else:
                for key in ("target_job_id", "sender_agent_id", "source"):
                    value = event_payload.get(key)
                    if not isinstance(value, str) or not value:
                        raise ValueError("subagent directive metadata is invalid")

    @staticmethod
    def _compact_legacy_request_event(
        event: HistoryEvent,
    ) -> tuple[HistoryEvent, bool]:
        if event.kind != "request_committed":
            return event, False
        replay = event.payload.get("replay")
        if not isinstance(replay, dict) or "items" not in replay:
            return event, False

        payload = dict(event.payload)
        payload["replay"] = {
            "schema_version": replay.get("schema_version"),
            "view_id": replay.get("view_id"),
            "cache_epoch": replay.get("cache_epoch", 0),
            "history_version": replay.get("history_version", 0),
            "model_profile": replay.get("model_profile"),
            "provider_family": replay.get("provider_family"),
            "request_mode": replay.get("request_mode"),
            "instruction_count": len(replay.get("instructions") or ()),
            "tool_count": len(replay.get("tools") or ()),
            "item_count": len(replay.get("items") or ()),
            "stable_prefix_hash": replay.get("stable_prefix_hash"),
            "canonical_payload_hash": replay.get("canonical_payload_hash"),
            "migrated_from_full_replay": True,
        }
        return replace(event, payload=payload), True
