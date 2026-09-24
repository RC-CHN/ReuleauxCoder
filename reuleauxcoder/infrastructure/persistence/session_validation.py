"""Pure schema validation and bounded restore issue facts for session payloads."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime

from reuleauxcoder.domain.context.manager import (
    MESSAGE_TOKEN_KEY,
)
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import (
    ReplayEnvelope,
    RequestEnvelope,
    validate_provider_message,
    validate_replay_payload,
)
from reuleauxcoder.domain.plan import PlanController
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRestoreIssue,
    is_safe_session_preview,
)
from reuleauxcoder.infrastructure.persistence.session_paths import (
    is_safe_session_id,
)


DEFAULT_SESSION_FINGERPRINT = "local"

_SESSION_STORAGE_SCHEMA_VERSION = 2

_MAX_REQUEST_RECORDS = 200

_MAX_RESTORE_ISSUES = 8

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


class SessionPayloadValidator:
    """Schema and trust checks without file I/O or writer state."""

    @staticmethod
    def _require_safe_session_id(session_id: object) -> None:
        if is_safe_session_id(session_id):
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
            not is_safe_session_id(metadata.id)
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

    @classmethod
    def _validate_legacy_session_payload(cls, payload: dict) -> None:
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
                cls._validate_runtime_state_payload(payload["runtime_state"])
            except ValueError:
                fail("SessionRuntimeStateValidationError")
        try:
            previous_seq = 0
            seen_event_ids: set[str] = set()
            for item in payload.get("history_events", ()):
                cls._validate_history_event_payload(
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
                    cls._validate_request_record_payload(
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
                    cls._validate_checkpoint_payload(
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
            stream_ids = event_payload.get("output_stream_ids", [])
            if not isinstance(stream_ids, list) or not all(
                isinstance(value, str) and value for value in stream_ids
            ):
                raise ValueError("history output acknowledgements are invalid")
            for key in ("steering_id", "attempt_id"):
                value = event_payload.get(key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError("history message control metadata is invalid")
        elif kind == "output_checkpoint":
            if (
                not isinstance(event_payload.get("stream_id"), str)
                or not event_payload["stream_id"]
                or event_payload.get("kind") not in ("response", "reasoning", "tool")
                or not isinstance(event_payload.get("text"), str)
                or not isinstance(event_payload.get("replace", False), bool)
                or any(
                    value is not None and not isinstance(value, str)
                    for value in (
                        event_payload.get("tool_call_id"),
                        event_payload.get("tool_name"),
                    )
                )
            ):
                raise ValueError("history output checkpoint is invalid")
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
            for key in ("steering_id", "turn_id"):
                value = event_payload.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError("history steering admission is invalid")
            content = event_payload.get("content")
            if not isinstance(content, (str, list)) or not content:
                raise ValueError("history steering content is invalid")
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
