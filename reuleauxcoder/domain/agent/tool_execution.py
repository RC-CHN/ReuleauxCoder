"""Tool execution - handles tool calls."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator, Mapping
from copy import deepcopy
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from difflib import get_close_matches
from enum import Enum
import math
from threading import Lock
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, List, cast

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.cancellation import CancellationView
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolArchiveReference,
    ToolDiagnostic,
    ToolDiff,
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetentionHint,
    ToolRetentionStrategy,
    ToolTruncation,
)
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalGrantCandidate,
    ApprovalGrantScope,
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.approval_subjects import approval_scope_key
from reuleauxcoder.domain.config.models import ApprovalRuleConfig
from reuleauxcoder.domain.approval_preview import (
    build_approval_preview,
    capture_approval_document,
    capture_workspace_document,
    diff_approval_documents,
)
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookDiagnostic,
    HookKind,
    HookPoint,
)
from reuleauxcoder.domain.workspace import WorkspaceRevision
from reuleauxcoder.extensions.tools.base import InterruptMode, Tool


_EXTERNAL_PATH_ARGUMENTS = {
    "edit_file": "file_path",
    "glob": "path",
    "grep": "path",
    "list_file": "path",
    "lsp": "filePath",
    "read_file": "file_path",
    "write_file": "file_path",
}
_EXTERNAL_MUTATION_TOOLS = frozenset({"edit_file", "write_file"})
_POST_EFFECT_FAILURE_LIMIT = 8
_POST_EFFECT_FAILURE_COUNT_LIMIT = 1_000_000
_PENDING_RUNTIME_FAILURE_LIMIT = 8
_RUNTIME_FAILURE_PUBLISH_ATTEMPTS = 2
_METADATA_MAX_DEPTH = 32
_METADATA_MAX_CONTAINER_ITEMS = 8_192
_METADATA_MAX_NODES = 16_384
_METADATA_MAX_INT_BITS = 256
_METADATA_MAX_STRING_BYTES = 1_048_576
_METADATA_MAX_TOTAL_STRING_BYTES = 2_097_152
_STRUCTURED_FACT_MAX_STRING_BYTES = 65_536
_STRUCTURED_FACT_MAX_TOTAL_STRING_BYTES = 2_097_152
_TOOL_DIAGNOSTIC_LIMIT = 512
_TEXT_VALIDATION_CHUNK_CHARS = 65_536
_FAILURE_FACT_KEYS = frozenset(
    {"failure_phase", "error_type", "effect_state", "completion_state", "retry_safety"}
)
_RUNTIME_METADATA_KEYS = _FAILURE_FACT_KEYS | {
    "post_effect_failures",
    "reported_effect_state",
}
_REPORTED_EFFECT_STATES = frozenset(
    {"not_started", "started", "completed", "unknown", "server_reported_failure"}
)
_VALID_TRUNCATION_STRATEGIES = frozenset(
    strategy.value for strategy in ToolRetentionStrategy
)
_UNRESOLVED_TOOL = object()


class InvalidAfterToolPrimaryOutcomeTransition(RuntimeError):
    pass


class InvalidToolResultProjection(TypeError):
    pass


class InvalidToolOutcomeProtocol(TypeError):
    pass


class MissingRuntimeIssueSink(RuntimeError):
    pass


def _tool_call_signature(tool_call: object) -> tuple[object, object, object]:
    try:
        tool_call_id = getattr(tool_call, "id")
        name = getattr(tool_call, "name")
        arguments = getattr(tool_call, "arguments")
        if not isinstance(tool_call_id, str) or not isinstance(name, str):
            raise TypeError
        if not isinstance(arguments, dict):
            raise TypeError
        return (
            _snapshot_json_value(tool_call_id),
            _snapshot_json_value(name),
            _snapshot_json_value(arguments),
        )
    except Exception:
        raise InvalidContextContributionResult from None


def _safe_exception_type(error: BaseException) -> str:
    return _safe_failure_error_type(type(error).__name__)


def _safe_failure_phase(phase: object) -> str:
    return (
        phase
        if isinstance(phase, str)
        and 0 < len(phase) <= 64
        and phase.isascii()
        and all(character.isalnum() or character in "._-:" for character in phase)
        else "post_effect"
    )


def _safe_failure_error_type(error_type: object) -> str:
    return (
        error_type
        if isinstance(error_type, str)
        and 0 < len(error_type) <= 64
        and error_type.isascii()
        and error_type.replace("_", "").isalnum()
        else "Exception"
    )


class _PostEffectFailureCollector:
    """Thread-safe aggregation with one deterministic bounded projection."""

    def __init__(self, *, limit: int = _POST_EFFECT_FAILURE_LIMIT) -> None:
        self._limit = max(2, limit)
        self._counts: dict[tuple[str, str], int] = {}
        self._overflow_count = 0
        self._lock = Lock()

    def record(self, phase: object, error_type: object, *, count: int = 1) -> None:
        fact = (_safe_failure_phase(phase), _safe_failure_error_type(error_type))
        safe_count = (
            min(count, _POST_EFFECT_FAILURE_COUNT_LIMIT)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
            else 1
        )
        with self._lock:
            current = self._counts.get(fact)
            if current is not None:
                self._counts[fact] = min(
                    current + safe_count,
                    _POST_EFFECT_FAILURE_COUNT_LIMIT,
                )
            elif len(self._counts) < self._limit - 1:
                self._counts[fact] = safe_count
            else:
                largest = max(self._counts)
                if fact < largest:
                    omitted = self._counts.pop(largest)
                    self._counts[fact] = safe_count
                else:
                    omitted = safe_count
                self._overflow_count = min(
                    self._overflow_count + omitted,
                    _POST_EFFECT_FAILURE_COUNT_LIMIT,
                )

    def omit(self, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return
        safe_count = min(count, _POST_EFFECT_FAILURE_COUNT_LIMIT)
        with self._lock:
            self._overflow_count = min(
                self._overflow_count + safe_count,
                _POST_EFFECT_FAILURE_COUNT_LIMIT,
            )

    def snapshot(self) -> tuple[tuple[str, str, int], ...]:
        with self._lock:
            facts = tuple(
                (phase, error_type, count)
                for (phase, error_type), count in sorted(self._counts.items())
            )
            overflow_count = self._overflow_count
        if overflow_count:
            facts += (("post_effect", "AdditionalFailuresOmitted", overflow_count),)
        return facts

    def drain(self) -> tuple[tuple[str, str, int], ...]:
        with self._lock:
            facts = tuple(
                (phase, error_type, count)
                for (phase, error_type), count in sorted(self._counts.items())
            )
            overflow_count = self._overflow_count
            self._counts.clear()
            self._overflow_count = 0
        if overflow_count:
            facts += (("post_effect", "AdditionalFailuresOmitted", overflow_count),)
        return facts

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._counts or self._overflow_count)


@dataclass(slots=True)
class _SnapshotBudget:
    nodes: int = 0
    string_bytes: int = 0

    def add_node(self) -> None:
        self.nodes += 1
        if self.nodes > _METADATA_MAX_NODES:
            raise InvalidToolOutcomeProtocol

    def add_string(self, value: str) -> str:
        # UTF-8 uses at least one byte per code point. Reject obviously
        # oversized input before allocating a second full-size buffer.
        if len(value) > _METADATA_MAX_STRING_BYTES:
            raise InvalidToolOutcomeProtocol
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise InvalidToolOutcomeProtocol from None
        size = len(encoded)
        if size > _METADATA_MAX_STRING_BYTES:
            raise InvalidToolOutcomeProtocol
        self.string_bytes += size
        if self.string_bytes > _METADATA_MAX_TOTAL_STRING_BYTES:
            raise InvalidToolOutcomeProtocol
        return value


def _validate_unbounded_text(value: str) -> None:
    """Validate retained output without allocating a second full-size buffer."""
    try:
        for offset in range(0, len(value), _TEXT_VALIDATION_CHUNK_CHARS):
            value[offset : offset + _TEXT_VALIDATION_CHUNK_CHARS].encode(
                "utf-8",
                errors="strict",
            )
    except UnicodeEncodeError:
        raise InvalidToolOutcomeProtocol from None


def _validate_fact_text(value: str) -> int:
    if len(value) > _STRUCTURED_FACT_MAX_STRING_BYTES:
        raise InvalidToolOutcomeProtocol
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise InvalidToolOutcomeProtocol from None
    size = len(encoded)
    if size > _STRUCTURED_FACT_MAX_STRING_BYTES:
        raise InvalidToolOutcomeProtocol
    return size


def _optional_int(value: object, *, nonnegative: bool = False) -> bool:
    return value is None or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value.bit_length() <= _METADATA_MAX_INT_BITS
        and (not nonnegative or value >= 0)
    )


def _snapshot_json_value(
    value: object,
    *,
    depth: int = 0,
    trail: set[int] | None = None,
    budget: _SnapshotBudget | None = None,
) -> object:
    """Copy extension-owned metadata into immutable, JSON-safe runtime data."""
    if depth > _METADATA_MAX_DEPTH:
        raise InvalidToolOutcomeProtocol
    owned_budget = budget if budget is not None else _SnapshotBudget()
    owned_budget.add_node()
    if isinstance(value, Enum):
        return _snapshot_json_value(
            value.value,
            depth=depth,
            trail=trail,
            budget=owned_budget,
        )
    if isinstance(value, str):
        return owned_budget.add_string(str(value))
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > _METADATA_MAX_INT_BITS:
            raise InvalidToolOutcomeProtocol
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidToolOutcomeProtocol
        return value
    if not isinstance(value, (Mapping, list, tuple)):
        raise InvalidToolOutcomeProtocol

    owned_trail = trail if trail is not None else set()
    identity = id(value)
    if identity in owned_trail:
        raise InvalidToolOutcomeProtocol
    owned_trail.add(identity)
    try:
        if isinstance(value, Mapping):
            copied: dict[str, object] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= _METADATA_MAX_CONTAINER_ITEMS:
                    raise InvalidToolOutcomeProtocol
                if not isinstance(key, str):
                    raise InvalidToolOutcomeProtocol
                owned_key = cast(
                    str,
                    _snapshot_json_value(
                        key,
                        depth=depth + 1,
                        trail=owned_trail,
                        budget=owned_budget,
                    ),
                )
                copied[owned_key] = _snapshot_json_value(
                    item,
                    depth=depth + 1,
                    trail=owned_trail,
                    budget=owned_budget,
                )
            return MappingProxyType(copied)
        copied_items: list[object] = []
        for index, item in enumerate(value):
            if index >= _METADATA_MAX_CONTAINER_ITEMS:
                raise InvalidToolOutcomeProtocol
            copied_items.append(
                _snapshot_json_value(
                    item,
                    depth=depth + 1,
                    trail=owned_trail,
                    budget=owned_budget,
                )
            )
        return tuple(copied_items)
    finally:
        owned_trail.remove(identity)


def _check_tool_outcome_protocol(outcome: ToolOutcome) -> Mapping[str, object]:
    """Reject malformed facts and snapshot only bounded runtime metadata."""
    diff, truncation, archive, retention = (
        outcome.diff,
        outcome.truncation,
        outcome.archive_reference,
        outcome.retention_hint,
    )
    valid = (
        isinstance(outcome.status, ToolOutcomeStatus)
        and (
            outcome.error_kind is None or isinstance(outcome.error_kind, ToolErrorKind)
        )
        and (outcome.status is ToolOutcomeStatus.SUCCEEDED)
        == (outcome.error_kind is None)
        and (
            outcome.status is not ToolOutcomeStatus.DENIED
            or outcome.error_kind is ToolErrorKind.DENIED
        )
        and (
            outcome.status is not ToolOutcomeStatus.CANCELLED
            or outcome.error_kind is ToolErrorKind.INTERRUPTED
        )
        and (outcome.summary is None or isinstance(outcome.summary, str))
        and (outcome.content is None or isinstance(outcome.content, str))
        and isinstance(outcome.stdout, str)
        and isinstance(outcome.stderr, str)
        and (outcome.model_content is None or isinstance(outcome.model_content, str))
        and _optional_int(outcome.exit_code)
        and isinstance(outcome.metadata, Mapping)
        and (
            outcome.duration_seconds is None
            or isinstance(outcome.duration_seconds, (int, float))
            and not isinstance(outcome.duration_seconds, bool)
            and math.isfinite(outcome.duration_seconds)
            and outcome.duration_seconds >= 0
        )
        and (diff is None or isinstance(diff, ToolDiff))
        and type(outcome.diagnostics) is tuple
        and len(outcome.diagnostics) <= _TOOL_DIAGNOSTIC_LIMIT
        and all(isinstance(item, ToolDiagnostic) for item in outcome.diagnostics)
        and (truncation is None or isinstance(truncation, ToolTruncation))
        and (archive is None or isinstance(archive, ToolArchiveReference))
        and isinstance(retention, ToolRetentionHint)
    )
    if not valid:
        raise InvalidToolOutcomeProtocol
    if diff is not None:
        valid &= (
            isinstance(diff.path, str)
            and isinstance(diff.unified, str)
            and all(
                _optional_int(value, nonnegative=True)
                for value in (diff.additions, diff.deletions, diff.original_chars)
            )
            and isinstance(diff.truncated, bool)
        )
    for item in outcome.diagnostics:
        valid &= (
            isinstance(item.path, str)
            and _optional_int(item.line, nonnegative=True)
            and item.line is not None
            and _optional_int(item.character, nonnegative=True)
            and item.character is not None
            and isinstance(item.message, str)
            and isinstance(item.severity, str)
            and (
                item.code is None
                or isinstance(item.code, str)
                or (_optional_int(item.code) and item.code is not None)
            )
            and (item.source is None or isinstance(item.source, str))
            and _optional_int(item.end_line, nonnegative=True)
            and _optional_int(item.end_character, nonnegative=True)
        )
    if truncation is not None:
        counts = (
            truncation.original_chars,
            truncation.original_lines,
            truncation.retained_chars,
            truncation.retained_lines,
        )
        valid &= (
            all(
                _optional_int(value, nonnegative=True) and value is not None
                for value in counts
            )
            and truncation.retained_chars <= truncation.original_chars
            and truncation.retained_lines <= truncation.original_lines
            and truncation.strategy in _VALID_TRUNCATION_STRATEGIES
            and isinstance(outcome.model_content, str)
        )
    if archive is not None:
        valid &= (
            isinstance(archive.path, str)
            and bool(archive.path)
            and isinstance(archive.media_type, str)
            and (
                archive.checksum_sha256 is None
                or isinstance(archive.checksum_sha256, str)
            )
            and _optional_int(archive.size_bytes, nonnegative=True)
        )
    valid &= isinstance(retention.strategy, ToolRetentionStrategy) and (
        retention.anchor_line is None
        or _optional_int(retention.anchor_line)
        and retention.anchor_line > 0
    )
    if not valid:
        raise InvalidToolOutcomeProtocol

    unbounded_text = [outcome.content, outcome.stdout, outcome.stderr]
    if outcome.model_content is not None:
        unbounded_text.append(outcome.model_content)
    if diff is not None:
        unbounded_text.append(diff.unified)
    for value in unbounded_text:
        if value is not None:
            _validate_unbounded_text(value)

    bounded_text = [outcome.summary]
    if diff is not None:
        bounded_text.append(diff.path)
    for item in outcome.diagnostics:
        bounded_text.extend(
            (
                item.path,
                item.message,
                item.severity,
                item.code if isinstance(item.code, str) else None,
                item.source,
            )
        )
    if truncation is not None:
        bounded_text.append(truncation.strategy)
    if archive is not None:
        bounded_text.extend((archive.path, archive.media_type, archive.checksum_sha256))
    structured_string_bytes = 0
    for value in bounded_text:
        if value is not None:
            structured_string_bytes += _validate_fact_text(value)
            if structured_string_bytes > _STRUCTURED_FACT_MAX_TOTAL_STRING_BYTES:
                raise InvalidToolOutcomeProtocol

    return cast(Mapping[str, object], _snapshot_json_value(outcome.metadata))


def _normalize_model_projection(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidToolResultProjection
    try:
        owned = str(value)
        _validate_unbounded_text(owned)
    except Exception:
        raise InvalidToolResultProjection from None
    return owned


def _normalize_tool_outcome(outcome: ToolOutcome) -> ToolOutcome:
    """Validate once and retain no live extension-owned projection or metadata."""
    if outcome.model_content is not None and not isinstance(outcome.model_content, str):
        raise InvalidToolResultProjection
    try:
        metadata = _check_tool_outcome_protocol(outcome)
    except InvalidToolOutcomeProtocol:
        raise
    except Exception:
        raise InvalidToolOutcomeProtocol from None
    try:
        model_text = outcome.model_text
    except Exception:
        raise InvalidToolResultProjection from None
    model_text = _normalize_model_projection(model_text)
    try:
        return replace(
            outcome,
            metadata=cast(Mapping[str, object], metadata),
            model_content=model_text,
        )
    except (TypeError, ValueError):
        raise InvalidToolOutcomeProtocol from None


_AFTER_FIXED_OUTCOME_FIELDS = (
    "status",
    "summary",
    "content",
    "stdout",
    "stderr",
    "diff",
    "exit_code",
    "duration_seconds",
    "error_kind",
    "retention_hint",
)
_AFTER_FIXED_CONTEXT_FIELDS = (
    "hook_point",
    "agent_id",
    "session_generation",
    "session_id",
    "turn_id",
    "trace_id",
    "round_index",
)


def _accepted_after_transform(
    original_context: AfterToolExecuteContext,
    transformed_context: object,
) -> ToolOutcome:
    """Accept presentation enrichment without yielding primary fact authority."""
    if not isinstance(transformed_context, AfterToolExecuteContext):
        raise InvalidAfterToolPrimaryOutcomeTransition
    if any(
        getattr(transformed_context, field) != getattr(original_context, field)
        for field in _AFTER_FIXED_CONTEXT_FIELDS
    ) or _tool_call_signature(transformed_context.tool_call) != _tool_call_signature(
        original_context.tool_call
    ):
        raise InvalidAfterToolPrimaryOutcomeTransition
    if not isinstance(transformed_context.outcome, ToolOutcome):
        raise InvalidToolResultProjection

    original = cast(ToolOutcome, original_context.outcome)
    transformed_outcome = transformed_context.outcome
    candidate = (
        original
        if transformed_outcome is original
        else _normalize_tool_outcome(transformed_outcome)
    )
    result = _normalize_model_projection(transformed_context.result)
    if result != candidate.model_text:
        candidate = candidate.with_model_projection(
            result,
            truncation=candidate.truncation,
            archive_reference=candidate.archive_reference,
        )
    if (
        any(
            getattr(candidate, field) != getattr(original, field)
            for field in _AFTER_FIXED_OUTCOME_FIELDS
        )
        or candidate.diagnostics[: len(original.diagnostics)] != original.diagnostics
    ):
        raise InvalidAfterToolPrimaryOutcomeTransition

    original_metadata = original.metadata
    if any(
        key not in candidate.metadata or candidate.metadata[key] != value
        for key, value in original_metadata.items()
    ):
        raise InvalidAfterToolPrimaryOutcomeTransition
    added_keys = candidate.metadata.keys() - original_metadata.keys()
    if added_keys & _RUNTIME_METADATA_KEYS:
        raise InvalidAfterToolPrimaryOutcomeTransition
    return candidate


def _safe_metadata_fact(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if not isinstance(value, str):
        return None
    if key == "error_type":
        return value if _safe_failure_error_type(value) == value else None
    if key == "effect_state":
        return (
            value
            if value in {"not_started", "started", "completed", "unknown"}
            else None
        )
    if key == "completion_state":
        return value if value in {"not_started", "completed", "uncertain"} else None
    if key == "retry_safety":
        return (
            value if value in {"safe_to_retry", "do_not_retry_automatically"} else None
        )
    return value if _safe_failure_phase(value) == value else None


def _with_failure_facts(
    outcome: ToolOutcome,
    *,
    phase: str,
    error_type: str,
    effect_state: str,
    completion_state: str,
    retry_safety: str,
    replace_existing: bool = False,
) -> ToolOutcome:
    """Fill canonical non-success facts without replacing tool-owned safe facts."""
    if outcome.success:
        if not replace_existing or not (
            outcome.metadata.keys() & _RUNTIME_METADATA_KEYS
        ):
            return outcome
        return replace(
            outcome,
            metadata=MappingProxyType(
                {
                    key: value
                    for key, value in outcome.metadata.items()
                    if key not in _RUNTIME_METADATA_KEYS
                }
            ),
        )
    metadata = {
        key: value
        for key, value in outcome.metadata.items()
        if not replace_existing or key not in _RUNTIME_METADATA_KEYS
    }
    defaults = {
        "failure_phase": _safe_failure_phase(phase),
        "error_type": _safe_failure_error_type(error_type),
        "effect_state": _safe_failure_phase(effect_state),
        "completion_state": _safe_failure_phase(completion_state),
        "retry_safety": _safe_failure_phase(retry_safety),
    }
    for key, default in defaults.items():
        if replace_existing or _safe_metadata_fact(metadata, key) is None:
            metadata[key] = default
    return replace(outcome, metadata=MappingProxyType(metadata))


def _cancelled_before_outcome(
    tool_name: str,
    phase: str,
    message: str,
) -> ToolOutcome:
    return _with_failure_facts(
        ToolOutcome(
            status=ToolOutcomeStatus.CANCELLED,
            summary=f"{tool_name} interrupted before execution",
            content=message,
            model_content=message,
            error_kind=ToolErrorKind.INTERRUPTED,
        ),
        phase=phase,
        error_type="ToolExecutionCancelled",
        effect_state="not_started",
        completion_state="not_started",
        retry_safety="safe_to_retry",
        replace_existing=True,
    )


def _with_canonical_failure_projection(outcome: ToolOutcome) -> ToolOutcome:
    if outcome.success:
        return outcome
    metadata = outcome.metadata
    line = (
        f"status={outcome.status.value} "
        f"phase={metadata['failure_phase']} "
        f"error_type={metadata['error_type']} "
        f"effect_state={metadata['effect_state']} "
        f"completion_state={metadata['completion_state']} "
        f"retry_safety={metadata['retry_safety']}"
    )
    marker = "[tool outcome facts]"
    text = outcome.model_text
    text = f"{text.rstrip()}\n\n{marker}\n{line}" if text else f"{marker}\n{line}"
    return outcome.with_model_projection(
        text,
        truncation=outcome.truncation,
        archive_reference=outcome.archive_reference,
    )


def _coerce_returned_outcome(
    raw_result: object, execution_seconds: float
) -> ToolOutcome:
    if isinstance(raw_result, ToolOutcome):
        outcome = raw_result
    else:
        if not isinstance(raw_result, str):
            raise InvalidToolOutcomeProtocol
        outcome = ToolOutcome.from_legacy(raw_result).with_duration(execution_seconds)
    normalized = _normalize_tool_outcome(outcome)
    phase = _safe_metadata_fact(normalized.metadata, "failure_phase") or "execute"
    error_type = (
        _safe_metadata_fact(normalized.metadata, "error_type") or "ToolReportedFailure"
    )
    reported_effect = normalized.metadata.get("effect_state")
    reported_effect = (
        reported_effect
        if isinstance(reported_effect, str)
        and reported_effect in _REPORTED_EFFECT_STATES
        else None
    )
    denied = normalized.status is ToolOutcomeStatus.DENIED
    projected = _with_failure_facts(
        normalized,
        phase=phase,
        error_type=error_type,
        effect_state="not_started" if denied else "unknown",
        completion_state="not_started" if denied else "uncertain",
        retry_safety="do_not_retry_automatically",
        replace_existing=True,
    )
    if normalized.success or reported_effect is None:
        return projected
    return replace(
        projected,
        metadata=MappingProxyType(
            {**projected.metadata, "reported_effect_state": reported_effect}
        ),
    )


def _completed_result_failure(error: BaseException) -> ToolOutcome:
    protocol_failure = isinstance(error, InvalidToolOutcomeProtocol)
    phase = "result_protocol" if protocol_failure else "result_projection"
    error_type = _safe_exception_type(error)
    problem = (
        "violated the result protocol" if protocol_failure else "could not be projected"
    )
    message = (
        f"The tool returned, but its result {problem} "
        f"(phase={phase}, error_type={error_type}, "
        "effect_state=unknown, completion_state=uncertain, "
        "retry_safety=do_not_retry_automatically). Do not retry solely because "
        "the result "
        f"{('protocol validation' if protocol_failure else 'projection')} failed."
    )
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        summary="Tool execution failed",
        content=message,
        model_content=message,
        error_kind=ToolErrorKind.INTERNAL,
        metadata={
            "failure_phase": phase,
            "error_type": error_type,
            "effect_state": "unknown",
            "completion_state": "uncertain",
            "retry_safety": "do_not_retry_automatically",
        },
    )


def _record_hook_diagnostics(
    failures: _PostEffectFailureCollector,
    diagnostics: object,
    *,
    hook_point: HookPoint,
    default_phase: str,
) -> None:
    if type(diagnostics) is not tuple:
        raise TypeError("observer diagnostics must be a tuple")
    retained = diagnostics[: _POST_EFFECT_FAILURE_LIMIT - 1]
    for diagnostic in retained:
        if (
            not isinstance(diagnostic, HookDiagnostic)
            or diagnostic.hook_point is not hook_point
            or diagnostic.hook_kind is not HookKind.OBSERVER
            or not isinstance(diagnostic.error_type, str)
            or not diagnostic.error_type
            or (diagnostic.phase is not None and not isinstance(diagnostic.phase, str))
        ):
            raise TypeError("observer returned an invalid diagnostic")
        failures.record(
            diagnostic.phase or default_phase,
            diagnostic.error_type,
        )
    failures.omit(len(diagnostics) - len(retained))


def _with_post_effect_failures(
    outcome: ToolOutcome,
    failures: _PostEffectFailureCollector,
) -> ToolOutcome:
    snapshot = failures.snapshot()
    if not snapshot:
        return outcome
    lines = [
        "[post-effect diagnostics]",
        "The tool outcome above remains authoritative. Do not retry the tool "
        "solely because secondary processing failed.",
    ]
    lines.extend(
        f"phase={phase} error_type={error_type} count={count}"
        for phase, error_type, count in snapshot
    )
    diagnostics = "\n".join(lines)
    model_text = outcome.model_text
    model_text = (
        f"{model_text.rstrip()}\n\n{diagnostics}" if model_text else diagnostics
    )
    facts = tuple(
        MappingProxyType({"phase": phase, "error_type": error_type, "count": count})
        for phase, error_type, count in snapshot
    )
    return replace(
        outcome.with_model_projection(
            model_text,
            truncation=outcome.truncation,
            archive_reference=outcome.archive_reference,
        ),
        metadata=MappingProxyType({**outcome.metadata, "post_effect_failures": facts}),
    )


@dataclass(slots=True)
class _PreEffectState:
    phase: str = "interrupt_check"
    effect_started: bool = False


class InvalidPreflightResult(RuntimeError):
    pass


class InvalidApprovalSubjectsResult(RuntimeError):
    pass


class InvalidApprovalScopeResult(RuntimeError):
    pass


class InvalidAuthorizationResult(RuntimeError):
    pass


class InvalidApprovalPreview(RuntimeError):
    pass


class InvalidApprovalDecisionResult(RuntimeError):
    pass


class InvalidContextContributionResult(RuntimeError):
    pass


def _validated_preflight_failure(value: object) -> ToolOutcome | None:
    if value is None:
        return None
    if not isinstance(value, ToolOutcome):
        raise InvalidPreflightResult
    try:
        normalized = _normalize_tool_outcome(value)
    except Exception:
        raise InvalidPreflightResult from None
    if normalized.success:
        raise InvalidPreflightResult
    return normalized


def _with_pre_effect_facts(
    outcome: ToolOutcome,
    *,
    phase: str,
    error_type: str,
) -> ToolOutcome:
    fact = (
        "Tool execution stopped before effects began "
        f"(phase={phase}, error_type={error_type}, effect_state=not_started)."
    )
    return _with_failure_facts(
        outcome.with_model_projection(
            f"{fact}\n\n{outcome.model_text}",
            truncation=outcome.truncation,
            archive_reference=outcome.archive_reference,
        ),
        phase=phase,
        error_type=error_type,
        effect_state="not_started",
        completion_state="not_started",
        retry_safety=(
            "do_not_retry_automatically"
            if outcome.status is ToolOutcomeStatus.DENIED
            else "safe_to_retry"
        ),
        replace_existing=True,
    )


def _approval_grant_candidates(
    tool,
    tc: "ToolCall",
    *,
    tool_source: str,
    mcp_server: str | None,
    profile: str | None,
    subjects: tuple[str, ...],
    scope_key: str,
) -> tuple[ApprovalGrantCandidate, ...]:
    build_scopes = getattr(tool, "approval_grant_scopes", None)
    raw_scopes = (
        build_scopes(deepcopy(tc.arguments), subjects) if callable(build_scopes) else ()
    )
    if not isinstance(raw_scopes, (tuple, list)):
        raise InvalidApprovalScopeResult
    scopes = tuple(raw_scopes)
    if not scopes and tool_source == "mcp":
        scopes = (
            ApprovalGrantScope(
                id="exact_tool",
                label="This MCP tool",
                description=(f"{mcp_server} · {tc.name}" if mcp_server else tc.name),
            ),
        )

    candidates: list[ApprovalGrantCandidate] = []
    seen_ids: set[str] = set()
    for scope in scopes:
        if (
            not isinstance(scope, ApprovalGrantScope)
            or not isinstance(scope.id, str)
            or not scope.id.strip()
            or scope.id in seen_ids
            or not isinstance(scope.label, str)
            or not scope.label.strip()
            or not isinstance(scope.description, str)
            or not isinstance(scope.patterns, (tuple, list))
            or any(
                not isinstance(pattern, str) or not pattern
                for pattern in scope.patterns
            )
            or not isinstance(scope.broad, bool)
        ):
            raise InvalidApprovalScopeResult
        seen_ids.add(scope.id)
        patterns: tuple[str | None, ...] = (
            tuple(scope.patterns) if scope.patterns else (None,)
        )
        rules = tuple(
            ApprovalRuleConfig(
                tool_name=tc.name,
                tool_source=tool_source,
                mcp_server=mcp_server,
                profile=profile,
                pattern=pattern,
                scope_key=scope_key,
                action="allow",
            )
            for pattern in patterns
        )
        if not rules:
            continue
        candidates.append(
            ApprovalGrantCandidate(
                id=scope.id,
                label=scope.label,
                description=scope.description,
                proposed_rules=rules,
                scope_key=scope_key,
                broad=scope.broad,
            )
        )
    return tuple(candidates)


def _external_workspace_target(tool, arguments: dict) -> str | None:
    """Detect an exact local file target outside the configured workspace."""
    tool_name = getattr(tool, "name", None)
    path_argument = (
        _EXTERNAL_PATH_ARGUMENTS.get(tool_name) if isinstance(tool_name, str) else None
    )
    if tool is None or path_argument is None:
        return None
    workspace = getattr(getattr(tool, "backend", None), "workspace", None)
    inspect_external = getattr(workspace, "external_path", None)
    grant_external = getattr(workspace, "grant_external_path", None)
    file_path = arguments.get(path_argument, ".")
    if not callable(inspect_external) or not callable(grant_external):
        return None
    if not isinstance(file_path, str) or not file_path:
        return None
    external = inspect_external(file_path)
    return str(external) if external is not None else None


@contextmanager
def _workspace_access_scope(workspace, external_target: str | None) -> Iterator[None]:
    """Grant an approved exact path for the duration of one local operation."""
    grant_external = getattr(workspace, "grant_external_path", None)
    if external_target is None or not callable(grant_external):
        yield
        return
    access_scope = cast(AbstractContextManager[object], grant_external(external_target))
    with access_scope:
        yield


@contextmanager
def _stream_handler_scope(
    backend,
    execution_context,
    handler,
) -> Iterator[None]:
    """Prefer backend-local execution state; preserve custom backend compatibility."""
    bind = getattr(backend, "stream_handler_scope", None)
    if callable(bind):
        scope = cast(AbstractContextManager[object], bind(handler))
        with scope:
            yield
        return
    previous = getattr(execution_context, "remote_stream_handler", None)
    if execution_context is not None:
        execution_context.remote_stream_handler = handler
    try:
        yield
    finally:
        if execution_context is not None:
            execution_context.remote_stream_handler = previous


@contextmanager
def _workspace_revision_scope(
    backend,
    revision: WorkspaceRevision | None,
) -> Iterator[None]:
    """Bind one call's prepared revision without mutating model arguments."""
    bind = getattr(backend, "workspace_revision_scope", None)
    if not callable(bind):
        yield
        return
    scope = cast(AbstractContextManager[object], bind(revision))
    with scope:
        yield


@contextmanager
def _tool_cancellation_scope(tool, backend, signal) -> Iterator[None]:
    """Install the same per-call signal on tool and backend compatibility paths."""
    tool_bind = getattr(tool, "execution_scope", None)
    backend_bind = getattr(backend, "cancellation_scope", None)
    tool_scope = (
        cast(AbstractContextManager[object], tool_bind(signal))
        if callable(tool_bind)
        else nullcontext()
    )
    backend_scope = (
        cast(AbstractContextManager[object], backend_bind(signal))
        if callable(backend_bind)
        else nullcontext()
    )
    with tool_scope:
        with backend_scope:
            yield


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self._pending_runtime_failures = _PostEffectFailureCollector(
            limit=_PENDING_RUNTIME_FAILURE_LIMIT
        )

    def _round_interrupt_epoch(self) -> int:
        read = getattr(self.agent, "round_interrupt_epoch", None)
        if not callable(read):
            return 0
        value = read()
        return value if isinstance(value, int) else 0

    def _stop_requested(self) -> bool:
        read = getattr(self.agent, "stop_requested", None)
        return bool(read()) if callable(read) else False

    def _stop_signal(self):
        signal = getattr(self.agent, "_stop_event", None)
        if signal is not None and callable(getattr(signal, "is_set", None)):
            return signal

        executor = self

        class _CompatibilityStopSignal:
            def is_set(self) -> bool:
                return executor._stop_requested()

        return _CompatibilityStopSignal()

    def _emit_post_effect_diagnostic(
        self,
        tc: "ToolCall",
        failure: tuple[str, str, int],
        failures: _PostEffectFailureCollector,
    ) -> None:
        phase, error_type, count = failure
        try:
            self.agent._emit_event(
                AgentEvent.diagnostic(
                    "Secondary tool processing failed after the primary outcome "
                    f"was fixed (phase={phase}, error_type={error_type}, "
                    f"count={count}).",
                    code="tool.post_effect_failure",
                    details={
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "phase": phase,
                        "error_type": error_type,
                        "count": count,
                    },
                )
            )
        except BaseException as error:
            # The model-facing result still carries the same safe fact. A
            # broken event sink cannot replace the primary tool outcome.
            self._capture_post_effect_failure(
                failures,
                "post_effect_diagnostic",
                error,
            )

    def _emit_batch_post_effect_diagnostic(
        self,
        failure: tuple[str, str, int],
        failures: _PostEffectFailureCollector,
        *,
        tool_count: int,
    ) -> None:
        phase, error_type, count = failure
        try:
            self.agent._emit_event(
                AgentEvent.diagnostic(
                    "Secondary parallel-batch processing failed after all tool "
                    "outcomes were fixed "
                    f"(phase={phase}, error_type={error_type}, count={count}).",
                    code="tool.post_effect_failure",
                    details={
                        "scope": "parallel_batch",
                        "tool_count": tool_count,
                        "phase": phase,
                        "error_type": error_type,
                        "count": count,
                    },
                )
            )
        except BaseException as error:
            self._capture_post_effect_failure(
                failures,
                "post_effect_diagnostic",
                error,
            )

    def _retain_runtime_failures(
        self,
        facts: tuple[tuple[str, str, int], ...],
    ) -> None:
        for phase, error_type, count in facts:
            if error_type == "AdditionalFailuresOmitted":
                self._pending_runtime_failures.omit(count)
            else:
                self._pending_runtime_failures.record(
                    phase,
                    error_type,
                    count=count,
                )

    def _publish_runtime_failures(
        self,
        facts: tuple[tuple[str, str, int], ...],
    ) -> tuple[int, bool]:
        published = 0
        for index, (phase, error_type, count) in enumerate(facts):
            try:
                sink = getattr(self.agent, "record_runtime_issue", None)
                if not callable(sink):
                    raise MissingRuntimeIssueSink
                accepted = sink(phase, error_type, "parallel_batch", count)
                if accepted is False:
                    raise MissingRuntimeIssueSink
            except BaseException as error:
                self._retain_runtime_failures(facts[index:])
                sink_failures = _PostEffectFailureCollector()
                self._capture_post_effect_failure(
                    sink_failures,
                    "runtime_issue_publish",
                    error,
                )
                self._retain_runtime_failures(sink_failures.snapshot())
                return published, True
            published += 1
        return published, False

    def _queue_batch_runtime_failure(
        self,
        phase: str,
        error: BaseException,
        *,
        tool_count: int,
    ) -> None:
        failures = _PostEffectFailureCollector()
        self._capture_post_effect_failure(failures, phase, error)
        self._emit_batch_post_effect_diagnostic(
            failures.snapshot()[0],
            failures,
            tool_count=tool_count,
        )
        self._publish_runtime_failures(failures.snapshot())

    def flush_pending_runtime_issues(self) -> int | None:
        """Publish retained safe facts before another model request."""
        published = 0
        for _ in range(_RUNTIME_FAILURE_PUBLISH_ATTEMPTS):
            facts = self._pending_runtime_failures.drain()
            if not facts:
                return published
            delivered, failed = self._publish_runtime_failures(facts)
            published += delivered
            if not failed:
                return published

        stop_failures = _PostEffectFailureCollector()
        self._request_stop_safely(stop_failures)
        self._retain_runtime_failures(stop_failures.snapshot())
        return None

    def _request_stop_safely(
        self,
        failures: _PostEffectFailureCollector,
    ) -> None:
        """Request shutdown without allowing ordinary observer faults to win."""
        try:
            already_requested = self._stop_requested()
        except BaseException as error:
            failures.record("post_effect", _safe_exception_type(error))
            already_requested = False
        if already_requested:
            return
        try:
            request_stop = getattr(self.agent, "request_stop", None)
            if callable(request_stop):
                request_stop()
        except BaseException as error:
            failures.record("post_effect", _safe_exception_type(error))

    def _capture_post_effect_failure(
        self,
        failures: _PostEffectFailureCollector,
        phase: str,
        error: BaseException,
    ) -> None:
        failures.record(phase, _safe_exception_type(error))
        if not isinstance(error, Exception):
            self._request_stop_safely(failures)

    def _finalize_post_effect_projection(
        self,
        outcome: ToolOutcome,
        failures: _PostEffectFailureCollector,
    ) -> ToolOutcome:
        """Add runtime-owned facts once, after every secondary stage has run."""
        if not outcome.success:
            outcome = _with_failure_facts(
                outcome,
                phase="tool_result",
                error_type="ToolReportedFailure",
                effect_state="unknown",
                completion_state="uncertain",
                retry_safety="do_not_retry_automatically",
            )
            outcome = _with_canonical_failure_projection(outcome)
        return _with_post_effect_failures(outcome, failures)

    def _publish_post_effect_outcome(
        self,
        tc: "ToolCall",
        tool_name: str,
        outcome: ToolOutcome,
        failures: _PostEffectFailureCollector,
    ) -> ToolOutcome:
        published = self._finalize_post_effect_projection(outcome, failures)
        try:
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_name,
                    published.model_text,
                    tool_call_id=tc.id,
                    outcome=published,
                )
            )
        except BaseException as error:
            self._capture_post_effect_failure(failures, "tool_end_event", error)
            published = self._finalize_post_effect_projection(outcome, failures)
        return published

    @staticmethod
    def _pre_effect_failure_outcome(
        phase: str,
        error: BaseException,
        *,
        cancelled: bool = False,
    ) -> ToolOutcome:
        error_type = _safe_exception_type(error)
        action = "interrupted" if cancelled else "failed"
        message = (
            f"Tool execution {action} before effects began "
            f"(phase={phase}, error_type={error_type}, effect_state=not_started, "
            "completion_state=not_started, retry_safety=safe_to_retry)."
        )
        return _with_failure_facts(
            ToolOutcome(
                status=(
                    ToolOutcomeStatus.CANCELLED
                    if cancelled
                    else ToolOutcomeStatus.FAILED
                ),
                summary=f"Tool pre-execution {action}",
                content=message,
                model_content=message,
                error_kind=(
                    ToolErrorKind.INTERRUPTED if cancelled else ToolErrorKind.INTERNAL
                ),
            ),
            phase=phase,
            error_type=error_type,
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="safe_to_retry",
            replace_existing=True,
        )

    @staticmethod
    def _pre_effect_denial_outcome(
        message: str,
        *,
        phase: str,
        error_type: str,
    ) -> ToolOutcome:
        facts = (
            "Tool execution denied before effects began "
            f"(phase={phase}, error_type={error_type}, effect_state=not_started)."
        )
        return _with_failure_facts(
            ToolOutcome(
                status=ToolOutcomeStatus.DENIED,
                summary="Tool execution denied",
                content=f"{facts}\n\n{message}",
                error_kind=ToolErrorKind.DENIED,
            ),
            phase=phase,
            error_type=error_type,
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

    def _unknown_tool_outcome(self, tool_name: str) -> ToolOutcome:
        available_names = sorted(
            {
                str(getattr(tool, "name", ""))
                for tool in self.agent.get_active_tools()
                if getattr(tool, "name", None)
            }
        )
        matches = get_close_matches(tool_name, available_names, n=3, cutoff=0.5)
        suggestion = (
            f" Closest available tool{'s' if len(matches) != 1 else ''}: "
            f"{', '.join(repr(name) for name in matches)}."
            if matches
            else ""
        )
        return _with_failure_facts(
            ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                summary=f"Unknown tool: {tool_name}",
                content=f"Error: unknown tool '{tool_name}'",
                model_content=(
                    f"Tool call rejected [unknown_tool]: '{tool_name}' is not available."
                    f"{suggestion}\n"
                    "Retry only with an exact currently available tool name, or continue "
                    "without a tool. Do not repeat the unavailable tool call."
                ),
                error_kind=ToolErrorKind.NOT_FOUND,
                metadata={
                    "preflight_code": "unknown_tool",
                    "requested_tool": tool_name,
                    "suggested_tools": tuple(matches),
                },
            ),
            phase="tool_lookup",
            error_type="UnknownTool",
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

    def execute(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None = None,
        _resolved_tool: object = _UNRESOLVED_TOOL,
        _resolution_error: BaseException | None = None,
    ) -> str:
        """Execute and publish exactly one authoritative result for one call."""
        started = time.monotonic()
        failures = _PostEffectFailureCollector()
        pre_effect = _PreEffectState()
        tool_call = tc
        try:
            pre_effect.phase = "tool_call_snapshot"
            _tool_call_signature(tc)
            tool_call = deepcopy(tc)
            _tool_call_signature(tool_call)
            outcome = self._execute_pipeline(
                tool_call,
                interrupt_baseline=interrupt_baseline,
                pre_effect=pre_effect,
                failures=failures,
                resolved_tool=_resolved_tool,
                resolution_error=_resolution_error,
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            outcome = (
                self._execution_failure_outcome(pre_effect.phase, error)
                if pre_effect.effect_started
                else self._pre_effect_failure_outcome(
                    pre_effect.phase,
                    error,
                    cancelled=True,
                )
                if isinstance(error, KeyboardInterrupt)
                else self._pre_effect_failure_outcome(pre_effect.phase, error)
            )

        try:
            monitor = getattr(self.agent, "performance_monitor", None)
            if monitor is not None:
                monitor.record(
                    "tool",
                    "call_total",
                    (time.monotonic() - started) * 1000,
                    status="ok" if outcome.success else "error",
                    attributes={
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "turn_id": self.agent._current_turn_id,
                    },
                )
        except BaseException as error:
            self._capture_post_effect_failure(failures, "call_total_monitor", error)
        if failures:
            self._emit_post_effect_diagnostic(
                tool_call, failures.snapshot()[0], failures
            )
        published = self._publish_post_effect_outcome(
            tool_call,
            tool_call.name,
            outcome,
            failures,
        )
        return published.model_text

    @staticmethod
    def _execution_failure_outcome(
        phase: str,
        error: BaseException,
    ) -> ToolOutcome:
        interrupted = isinstance(error, KeyboardInterrupt)
        action = "interrupted" if interrupted else "failed"
        error_type = _safe_exception_type(error)
        message = (
            f"Tool execution {action} (phase={phase}, error_type={error_type}, "
            "effect_state=started, completion_state=uncertain, "
            "retry_safety=do_not_retry_automatically). The tool may have produced "
            "partial effects; do not retry automatically."
        )
        return _with_failure_facts(
            ToolOutcome(
                status=(
                    ToolOutcomeStatus.CANCELLED
                    if interrupted
                    else ToolOutcomeStatus.FAILED
                ),
                summary=f"Tool execution {action}",
                content=message,
                model_content=message,
                error_kind=(
                    ToolErrorKind.INTERRUPTED
                    if interrupted
                    else ToolErrorKind.EXECUTION
                ),
            ),
            phase=phase,
            error_type=error_type,
            effect_state="started",
            completion_state="uncertain",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

    def _execute_pipeline(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None,
        pre_effect: _PreEffectState,
        failures: _PostEffectFailureCollector,
        resolved_tool: object,
        resolution_error: BaseException | None,
    ) -> ToolOutcome:
        """Execute a single tool call."""
        authorized_signature = _tool_call_signature(tc)
        if interrupt_baseline is not None and (
            self._stop_requested() or self._round_interrupt_epoch() > interrupt_baseline
        ):
            reason = (
                "user steering"
                if self._round_interrupt_epoch() > interrupt_baseline
                and not self._stop_requested()
                else "turn cancellation"
            )
            message = (
                f"Tool execution interrupted before execution ({reason}; "
                "phase=initial_cancel_check, error_type=ToolExecutionCancelled, "
                "effect_state=not_started, completion_state=not_started, "
                "retry_safety=safe_to_retry)."
            )
            return _cancelled_before_outcome(
                tc.name,
                "initial_cancel_check",
                message,
            )
        reviewed_diff: str | None = None
        approval_workspace_changes: list[str] = []
        expected_workspace_revision: WorkspaceRevision | None = None
        pre_effect.phase = "tool_lookup"
        if resolution_error is not None:
            raise resolution_error
        resolved = (
            self.agent.get_tool(tc.name)
            if resolved_tool is _UNRESOLVED_TOOL
            else resolved_tool
        )
        pre_effect.phase = "tool_scope"
        if resolved is None:
            has_registered_tool = getattr(self.agent, "has_registered_tool", None)
            if (
                callable(has_registered_tool)
                and has_registered_tool(tc.name)
                and not self.agent.is_tool_allowed_in_mode(tc.name)
            ):
                mode_name = self.agent.active_mode or "default"
                message = (
                    f"Tool '{tc.name}' is not available in current mode "
                    f"'{mode_name}'"
                )
                return self._pre_effect_denial_outcome(
                    message,
                    phase="mode_policy",
                    error_type="ToolModeDenied",
                )
            return self._unknown_tool_outcome(tc.name)
        tool = cast(Tool, resolved)

        pre_effect.phase = "mode_policy"
        if not self.agent.is_tool_allowed_in_mode(tc.name):
            mode_name = self.agent.active_mode or "default"
            suggested_modes = self.agent.suggest_modes_for_tool(tc.name)
            if suggested_modes:
                suggestions = ", ".join(
                    f"/mode switch {name}" for name in suggested_modes
                )
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'. "
                    f"Ask user to switch mode first: {suggestions}"
                )
            else:
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'"
                )
            return self._pre_effect_denial_outcome(
                message,
                phase="mode_policy",
                error_type="ToolModeDenied",
            )

        approval_subjects: tuple[str, ...] = ()
        if tool is not None:
            pre_effect.phase = "schema_validation"
            schema_failure = _validated_preflight_failure(
                tool.preflight_validate(
                    deepcopy(tc.arguments),
                    schema_only=True,
                )
            )
            if schema_failure is not None:
                return _with_pre_effect_facts(
                    schema_failure,
                    phase="schema_validation",
                    error_type="ToolPreflightRejected",
                )
            pre_effect.phase = "approval_subjects"
            build_subjects = getattr(tool, "approval_subjects", None)
            if callable(build_subjects):
                built_subjects = build_subjects(deepcopy(tc.arguments))
                if (
                    not isinstance(built_subjects, (tuple, list))
                    or any(
                        not isinstance(subject, str) or not subject.strip()
                        for subject in built_subjects
                    )
                    or len(set(built_subjects)) != len(built_subjects)
                ):
                    raise InvalidApprovalSubjectsResult
                approval_subjects = tuple(built_subjects)

        pre_effect.phase = "approval_scope"
        current_scope_key = approval_scope_key(
            tool,
            session_id=self.agent.current_session_id,
        )
        if not isinstance(current_scope_key, str) or not current_scope_key:
            raise InvalidApprovalScopeResult
        pre_effect.phase = "authorization_context"
        before_context = BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            agent_id=self.agent.agent_id,
            session_generation=self.agent.session_generation,
            session_id=self.agent.current_session_id,
            turn_id=self.agent._current_turn_id,
            tool_call=tc,
            round_index=self.agent.state.current_round,
            metadata={
                "tool_source": getattr(
                    tool, "tool_source", "builtin" if tool is not None else "unknown"
                ),
                "mcp_server": getattr(tool, "server_name", None),
                "tool_description": getattr(tool, "description", None),
                "tool_schema": getattr(tool, "parameters", None),
                "effect_class": getattr(tool, "effect_class", None),
                "profile": getattr(tool, "approval_profile", None),
                "approval_subjects": approval_subjects,
                "approval_scope_key": current_scope_key,
            },
        )

        # Fixed core pipeline: lookup -> schema validation -> approval subjects
        # -> authorize -> environment validation -> approve -> contribute ->
        # execute -> process outcome -> observe -> publish. Extension code
        # cannot reorder or bypass the core stages.
        pre_effect.phase = "authorize"
        authorization_context = deepcopy(before_context)
        raw_guard_decisions = self.agent.extension_runtime.authorize_tool(
            authorization_context
        )
        if _tool_call_signature(
            authorization_context.tool_call
        ) != _tool_call_signature(tc):
            raise InvalidAuthorizationResult
        if not isinstance(raw_guard_decisions, (tuple, list)) or any(
            not isinstance(decision, GuardDecision)
            or not isinstance(decision.allowed, bool)
            or (decision.reason is not None and not isinstance(decision.reason, str))
            or (decision.warning is not None and not isinstance(decision.warning, str))
            or not isinstance(decision.requires_approval, bool)
            for decision in raw_guard_decisions
        ):
            raise InvalidAuthorizationResult
        guard_decisions = tuple(raw_guard_decisions)
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            message = denied.reason or f"Tool '{tc.name}' blocked by guard hook"
            return self._pre_effect_denial_outcome(
                message,
                phase="authorize",
                error_type="ToolAuthorizationDenied",
            )

        for decision in guard_decisions:
            if decision.warning:
                try:
                    self.agent._emit_event(
                        AgentEvent.diagnostic(
                            decision.warning,
                            code="tool.guard_warning",
                            details={"tool_name": tc.name, "tool_call_id": tc.id},
                        )
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "guard_warning_observer",
                        error,
                    )

        pre_effect.phase = "workspace_target"
        external_target = _external_workspace_target(tool, tc.arguments)
        external_mutation = (
            external_target is not None and tc.name in _EXTERNAL_MUTATION_TOOLS
        )
        pre_effect.phase = "tool_environment"
        backend = getattr(tool, "backend", None)
        workspace = getattr(backend, "workspace", None)
        if tool is not None:
            if not external_mutation:
                pre_effect.phase = "environment_preflight"
                with _workspace_access_scope(workspace, external_target):
                    preflight_failure = _validated_preflight_failure(
                        tool.preflight_validate(deepcopy(tc.arguments))
                    )
                if preflight_failure is not None:
                    return _with_pre_effect_facts(
                        preflight_failure,
                        phase="environment_preflight",
                        error_type="ToolPreflightRejected",
                    )
                pre_effect.phase = "document_snapshot"
                with _workspace_access_scope(workspace, external_target):
                    prepared_document = capture_workspace_document(
                        tc.name,
                        tc.arguments,
                        workspace=workspace,
                    )
                if prepared_document is not None:
                    expected_workspace_revision = prepared_document.revision

        pre_effect.phase = "approval_policy"
        if external_mutation:
            approval_required = GuardDecision.require_approval(
                "Target is outside the workspace. Approval grants this tool call "
                f"access to one exact file only: {external_target}"
            )
        else:
            approval_required = next(
                (d for d in guard_decisions if d.requires_approval), None
            )
        if approval_required is not None:
            pre_effect.phase = "approval_provider"
            provider = self.agent.approval_provider
            if provider is None:
                message = (
                    approval_required.reason
                    or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
                )
                return self._pre_effect_denial_outcome(
                    message,
                    phase="approval_provider",
                    error_type="ApprovalProviderUnavailable",
                )
            try:
                # The arguments are canonical and identical across attempts;
                # only the preview is refreshed when the file changes on disk.
                approval_tool_args = deepcopy(tc.arguments)
                for approval_attempt in range(3):
                    tool_source = str(
                        before_context.metadata.get("tool_source") or "unknown"
                    )
                    mcp_server = before_context.metadata.get("mcp_server")
                    profile = before_context.metadata.get("profile")
                    pre_effect.phase = "approval_scope"
                    grant_candidates = _approval_grant_candidates(
                        tool,
                        tc,
                        tool_source=tool_source,
                        mcp_server=(
                            str(mcp_server) if mcp_server is not None else None
                        ),
                        profile=str(profile) if profile is not None else None,
                        subjects=approval_subjects,
                        scope_key=current_scope_key,
                    )
                    if external_mutation:
                        grant_candidates = tuple(
                            candidate
                            for candidate in grant_candidates
                            if not candidate.broad
                        )
                    pre_effect.phase = "approval_request"
                    approval_request = ApprovalRequest(
                        tool_name=tc.name,
                        tool_args=approval_tool_args,
                        tool_source=tool_source,
                        mcp_server=(
                            str(mcp_server) if mcp_server is not None else None
                        ),
                        reason=approval_required.reason,
                        effect_class=before_context.metadata.get("effect_class"),
                        profile=str(profile) if profile is not None else None,
                        subjects=approval_subjects,
                        scope_key=current_scope_key,
                        grant_candidates=grant_candidates,
                        metadata={
                            "agent_id": self.agent.agent_id,
                            "session_generation": self.agent.session_generation,
                            "turn_id": self.agent._current_turn_id,
                            "tool_call_id": tc.id,
                            "approval_attempt": approval_attempt,
                            "workspace_changed_during_approval": bool(
                                approval_workspace_changes
                            ),
                            "invocation_reason": tc.arguments.get("reason"),
                            "policy_reason": approval_required.reason,
                            "is_subagent": bool(
                                getattr(self.agent, "subagent_job_id", None)
                            ),
                            "subagent_job_id": getattr(
                                self.agent, "subagent_job_id", None
                            ),
                            "subagent_mode": getattr(self.agent, "subagent_mode", None),
                            "subagent_task": getattr(self.agent, "subagent_task", None),
                            "external_workspace_path": external_target,
                            "workspace_root": (
                                str(getattr(workspace, "root", ""))
                                if external_target is not None
                                else None
                            ),
                            "force_human_review": external_mutation,
                            "approval_subjects": approval_subjects,
                        },
                    )
                    if not external_mutation and isinstance(
                        tc.arguments.get("reason"), str
                    ):
                        approval_request.reason = tc.arguments["reason"].strip()
                    pre_effect.phase = "approval_preview"
                    with _workspace_access_scope(workspace, external_target):
                        before_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                        if tc.name == "write_file" and before_approval is not None:
                            approval_request.metadata["approval_operation"] = (
                                "Create file"
                                if before_approval.content is None
                                else "Overwrite file"
                            )
                        elif tc.name == "edit_file":
                            approval_request.metadata["approval_operation"] = (
                                "Edit file"
                            )
                        elif tc.name == "shell":
                            approval_request.metadata["approval_operation"] = (
                                "Run command"
                            )
                        preview = build_approval_preview(
                            approval_request, workspace=workspace
                        )
                        if not isinstance(preview, ApprovalPreview):
                            raise InvalidApprovalPreview
                        approval_request.preview = preview
                    pre_effect.phase = "approval_provider"
                    try:
                        monitor = getattr(self.agent, "performance_monitor", None)
                    except BaseException as error:
                        monitor = None
                        self._capture_post_effect_failure(
                            failures,
                            "approval_monitor",
                            error,
                        )
                    approval_started = time.monotonic()
                    approval_status = "ok"
                    try:
                        decision = provider.request_approval(approval_request)
                    except BaseException:
                        approval_status = "error"
                        raise
                    finally:
                        if monitor is not None:
                            try:
                                monitor.record(
                                    "tool",
                                    "approval_wait",
                                    (time.monotonic() - approval_started) * 1000,
                                    status=approval_status,
                                    attributes={
                                        "tool_name": tc.name,
                                        "tool_call_id": tc.id,
                                        "approval_attempt": approval_attempt + 1,
                                        "turn_id": self.agent._current_turn_id,
                                    },
                                )
                            except BaseException as error:
                                self._capture_post_effect_failure(
                                    failures,
                                    "approval_monitor",
                                    error,
                                )
                    pre_effect.phase = "approval_decision"
                    if (
                        not isinstance(decision, ApprovalDecision)
                        or decision.mode
                        not in {"allow_once", "allow_session", "deny_once"}
                        or (
                            decision.reason is not None
                            and not isinstance(decision.reason, str)
                        )
                        or not isinstance(decision.reviewed, bool)
                        or (
                            decision.grant is not None
                            and not isinstance(decision.grant, ApprovalGrantCandidate)
                        )
                        or (decision.mode == "allow_session" and decision.grant is None)
                    ):
                        raise InvalidApprovalDecisionResult
                    if not decision.approved:
                        break
                    pre_effect.phase = "approval_revalidation"
                    with _workspace_access_scope(workspace, external_target):
                        after_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                    if (before_approval is None and after_approval is None) or (
                        before_approval is not None
                        and after_approval is not None
                        and before_approval.same_content(after_approval)
                    ):
                        if after_approval is not None:
                            expected_workspace_revision = after_approval.revision
                        break
                    if before_approval is not None and after_approval is not None:
                        pre_effect.phase = "approval_revalidation"
                        approval_workspace_changes.append(
                            diff_approval_documents(before_approval, after_approval)
                        )
                else:
                    message = (
                        f"Tool '{tc.name}' target kept changing during approval; "
                        "retry after editor changes settle"
                    )
                    return _with_failure_facts(
                        ToolOutcome(
                            status=ToolOutcomeStatus.FAILED,
                            content=(
                                "Tool execution failed before effects began "
                                "(phase=approval_revalidation, "
                                "error_type=ApprovalTargetUnstable, "
                                "effect_state=not_started).\n\n"
                                f"{message}"
                            ),
                            error_kind=ToolErrorKind.EXECUTION,
                        ),
                        phase="approval_revalidation",
                        error_type="ApprovalTargetUnstable",
                        effect_state="not_started",
                        completion_state="not_started",
                        retry_safety="safe_to_retry",
                        replace_existing=True,
                    )
            except (KeyboardInterrupt, EOFError) as error:
                message = f"Tool '{tc.name}' approval interrupted by user"
                return _with_failure_facts(
                    ToolOutcome(
                        status=ToolOutcomeStatus.CANCELLED,
                        content=message,
                        error_kind=ToolErrorKind.INTERRUPTED,
                    ),
                    phase="approval_provider",
                    error_type=(
                        "ApprovalInterrupted"
                        if isinstance(error, KeyboardInterrupt)
                        else "ApprovalInputClosed"
                    ),
                    effect_state="not_started",
                    completion_state="not_started",
                    retry_safety="safe_to_retry",
                    replace_existing=True,
                )

            pre_effect.phase = "approval_decision"
            if not decision.approved:
                message = (
                    decision.reason or f"Tool '{tc.name}' denied by approval provider"
                )
                return self._pre_effect_denial_outcome(
                    message,
                    phase="approval_decision",
                    error_type="ApprovalDenied",
                )
            if decision.reviewed and approval_request.preview is not None:
                reviewed_diff = next(
                    (
                        str(section.content)
                        for section in approval_request.preview.sections
                        if section.kind is ApprovalSectionKind.DIFF
                    ),
                    None,
                )

            if external_mutation and tool is not None:
                pre_effect.phase = "post_approval_preflight"
                with _workspace_access_scope(workspace, external_target):
                    preflight_failure = _validated_preflight_failure(
                        tool.preflight_validate(deepcopy(tc.arguments))
                    )
                if preflight_failure is not None:
                    return _with_pre_effect_facts(
                        preflight_failure,
                        phase="post_approval_preflight",
                        error_type="ToolPreflightRejected",
                    )

        pre_effect.phase = "context_contribution"
        contributed_context = self.agent.extension_runtime.contribute_tool_context(
            deepcopy(before_context)
        )
        if not isinstance(contributed_context, BeforeToolExecuteContext) or (
            _tool_call_signature(contributed_context.tool_call)
            != _tool_call_signature(tc)
        ):
            raise InvalidContextContributionResult
        before_context = contributed_context
        # Restore the canonical tool call into the context. ``tc`` is already
        # the pipeline's private snapshot, and the only consumer below is the
        # observer hand-off, which deep-copies the whole context again.
        before_context.tool_call = tc
        pre_effect.phase = "before_execute_observer"
        try:
            observer_diagnostics = self.agent.extension_runtime.observe(
                HookPoint.BEFORE_TOOL_EXECUTE, deepcopy(before_context)
            )
            _record_hook_diagnostics(
                failures,
                observer_diagnostics,
                hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
                default_phase="before_execute_observer",
            )
        except BaseException as error:
            self._capture_post_effect_failure(
                failures,
                "before_execute_observer",
                error,
            )

        pre_effect.phase = "context_result"
        tool_call = tc
        if _tool_call_signature(tool_call) != authorized_signature:
            raise InvalidAuthorizationResult

        pre_effect.phase = "final_cancel_check"
        stop_requested = getattr(self.agent, "stop_requested", None)
        if callable(stop_requested) and stop_requested():
            message = (
                f"Tool '{tc.name}' cancelled before execution "
                "(phase=final_cancel_check, error_type=ToolExecutionCancelled, "
                "effect_state=not_started, completion_state=not_started, "
                "retry_safety=safe_to_retry)."
            )
            return _cancelled_before_outcome(
                tc.name,
                "final_cancel_check",
                message,
            )

        pre_effect.phase = "execution_setup"
        tool_returned = False
        raw_result: object = _UNRESOLVED_TOOL
        execution_seconds = 0.0
        authoritative_outcome: ToolOutcome | None = None
        try:
            backend = getattr(tool, "backend", None)
            if interrupt_baseline is None:
                interrupt_baseline = self._round_interrupt_epoch()
            interrupt_mode = getattr(tool, "interrupt_mode", InterruptMode.LET_FINISH)
            cancellation = (
                None
                if interrupt_mode is InterruptMode.DETACH
                else CancellationView(
                    self._stop_signal(),
                    self._round_interrupt_epoch,
                    interrupt_baseline,
                    include_round_interrupt=(
                        interrupt_mode is InterruptMode.CANCEL_WITH_PARTIAL
                    ),
                )
            )
            execution_context = getattr(backend, "context", None)
            outer_stream_handler = getattr(
                execution_context, "remote_stream_handler", None
            )

            def stream_handler(tool_name, chunk) -> None:
                try:
                    from reuleauxcoder.domain.process_output import (
                        terminal_safe_display,
                    )

                    self.agent._emit_event(
                        AgentEvent.tool_output_delta(
                            tool_name,
                            terminal_safe_display(str(getattr(chunk, "data", ""))),
                            stream=str(getattr(chunk, "chunk_type", "stdout")),
                            tool_call_id=tc.id,
                        )
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(failures, "stream_event", error)
                if callable(outer_stream_handler):
                    try:
                        outer_stream_handler(tool_name, chunk)
                    except BaseException as error:
                        self._capture_post_effect_failure(
                            failures, "stream_observer", error
                        )

            execution_started = time.monotonic()
            primary_execution_error: BaseException | None = None
            try:
                try:
                    with _tool_cancellation_scope(tool, backend, cancellation):
                        with _stream_handler_scope(
                            backend,
                            execution_context,
                            stream_handler,
                        ):
                            with _workspace_revision_scope(
                                backend,
                                expected_workspace_revision,
                            ):
                                bind_execution = getattr(tool, "bind_execution", None)
                                if callable(bind_execution):
                                    bind_execution(
                                        tool_call_id=tc.id,
                                        session_generation=self.agent.session_generation,
                                    )
                                execution_workspace = getattr(
                                    backend, "workspace", None
                                )
                                with _workspace_access_scope(
                                    execution_workspace, external_target
                                ):
                                    pre_effect.phase = "execute"
                                    pre_effect.effect_started = True
                                    try:
                                        raw_result = tool.execute(**tool_call.arguments)
                                    except BaseException as error:
                                        primary_execution_error = error
                                        raise
                                    tool_returned = True
                except BaseException as error:
                    if primary_execution_error is not None:
                        if error is not primary_execution_error:
                            self._capture_post_effect_failure(
                                failures,
                                "execution_cleanup",
                                error,
                            )
                        raise primary_execution_error
                    if not tool_returned:
                        raise
                    self._capture_post_effect_failure(
                        failures,
                        "execution_cleanup",
                        error,
                    )
                if primary_execution_error is not None:
                    # A compatibility context manager may suppress the tool's
                    # exception. The tool failure remains the primary fact.
                    raise primary_execution_error
            finally:
                execution_seconds = time.monotonic() - execution_started
                try:
                    monitor = getattr(self.agent, "performance_monitor", None)
                    if monitor is not None:
                        monitor.record(
                            "tool",
                            "execute",
                            execution_seconds * 1000,
                            attributes={
                                "tool_name": tool_call.name,
                                "tool_call_id": tc.id,
                                "turn_id": self.agent._current_turn_id,
                            },
                        )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "execute_monitor",
                        error,
                    )
                try:
                    git_monitor = getattr(self.agent, "git_monitor", None)
                    if git_monitor is not None and (
                        getattr(tool, "effect_class", None)
                        in {"filesystem_mutation", "process_execution"}
                        or tool_call.name == "shell_session"
                    ):
                        git_monitor.invalidate()
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "git_invalidate",
                        error,
                    )
            outcome = _coerce_returned_outcome(raw_result, execution_seconds)
            authoritative_outcome = outcome
            if approval_workspace_changes:
                try:
                    change_report = "\n\n".join(
                        item for item in approval_workspace_changes if item
                    )
                    notice = (
                        "[workspace changed while approval was pending; "
                        "preview was refreshed]"
                    )
                    if change_report:
                        notice += f"\n{change_report}"
                    outcome = replace(
                        outcome.with_model_projection(
                            f"{outcome.model_text}\n\n{notice}",
                            truncation=outcome.truncation,
                            archive_reference=outcome.archive_reference,
                        ),
                        metadata=MappingProxyType(
                            {
                                **outcome.metadata,
                                "workspace_changed_during_approval": True,
                            }
                        ),
                    )
                    authoritative_outcome = outcome
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "approval_projection",
                        error,
                    )
            try:
                if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                    self.agent.runtime_working_directory = str(shell_cwd)
            except BaseException as error:
                self._capture_post_effect_failure(
                    failures,
                    "working_directory_sync",
                    error,
                )

            after_context: AfterToolExecuteContext | None = None
            try:
                after_context = AfterToolExecuteContext(
                    hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                    agent_id=self.agent.agent_id,
                    session_generation=self.agent.session_generation,
                    session_id=self.agent.current_session_id,
                    turn_id=self.agent._current_turn_id,
                    # No copy needed here: no extension code runs between this
                    # construction and the transform/observer hand-offs below,
                    # and each of those receives its own deep copy.
                    tool_call=tool_call,
                    result=outcome.model_text,
                    outcome=outcome,
                    round_index=self.agent.state.current_round,
                )
            except BaseException as error:
                self._capture_post_effect_failure(
                    failures,
                    "after_tool_context",
                    error,
                )

            if after_context is not None:
                transform_input_outcome = outcome
                try:
                    transformed_context = (
                        self.agent.extension_runtime.process_tool_outcome(
                            replace(
                                after_context,
                                tool_call=deepcopy(after_context.tool_call),
                            )
                        )
                    )
                    outcome = _accepted_after_transform(
                        after_context,
                        transformed_context,
                    )
                    if reviewed_diff is not None and (
                        outcome.diff is not None
                        and outcome.diff.unified == reviewed_diff
                    ):
                        outcome = replace(
                            outcome,
                            metadata=MappingProxyType(
                                {**outcome.metadata, "diff_reviewed": True}
                            ),
                        )
                    authoritative_outcome = outcome
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "after_tool_transform",
                        error,
                    )
                    outcome = transform_input_outcome
                    authoritative_outcome = outcome

                try:
                    observer_context = replace(
                        after_context,
                        tool_call=deepcopy(tool_call),
                        result=outcome.model_text,
                        outcome=outcome,
                    )
                    observer_diagnostics = self.agent.extension_runtime.observe(
                        HookPoint.AFTER_TOOL_EXECUTE, observer_context
                    )
                    _record_hook_diagnostics(
                        failures,
                        observer_diagnostics,
                        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                        default_phase="after_tool_observer",
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "after_tool_observer",
                        error,
                    )
            if outcome.archive_reference is not None:
                try:
                    self.agent.history_ledger.append(
                        "artifact_stored",
                        {
                            "tool_call_id": tc.id,
                            "tool_name": tool_call.name,
                            "artifact": {
                                "path": outcome.archive_reference.path,
                                "media_type": outcome.archive_reference.media_type,
                                "checksum_sha256": (
                                    outcome.archive_reference.checksum_sha256
                                ),
                                "size_bytes": outcome.archive_reference.size_bytes,
                            },
                            "original_lines": (
                                outcome.truncation.original_lines
                                if outcome.truncation
                                else None
                            ),
                            "original_chars": (
                                outcome.truncation.original_chars
                                if outcome.truncation
                                else None
                            ),
                        },
                        agent_id=self.agent.agent_id,
                        turn_id=self.agent._current_turn_id,
                        api_round_id=(
                            f"{self.agent._current_turn_id}:"
                            f"{self.agent.state.current_round}"
                            if self.agent._current_turn_id is not None
                            else None
                        ),
                        artifact_refs=(outcome.archive_reference.path,),
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "artifact_ledger",
                        error,
                    )
            return outcome
        except BaseException as error:
            if not pre_effect.effect_started:
                raise

            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            result_failure = tool_returned and (
                isinstance(
                    error,
                    (InvalidToolOutcomeProtocol, InvalidToolResultProjection),
                )
                or (authoritative_outcome is None and not isinstance(error, Exception))
            )
            if authoritative_outcome is None and tool_returned:
                if result_failure:
                    authoritative_outcome = _completed_result_failure(error)
                else:
                    try:
                        authoritative_outcome = _coerce_returned_outcome(
                            raw_result,
                            execution_seconds,
                        )
                    except BaseException as result_error:
                        if not isinstance(result_error, Exception):
                            self._request_stop_safely(failures)
                        authoritative_outcome = _completed_result_failure(result_error)
            if authoritative_outcome is not None:
                if not result_failure:
                    self._capture_post_effect_failure(
                        failures,
                        "post_effect",
                        error,
                    )
                return authoritative_outcome
            return self._execution_failure_outcome(pre_effect.phase, error)

    def execute_parallel(
        self,
        tool_calls: List["ToolCall"],
        *,
        interrupt_baseline: int | None = None,
    ) -> List[str]:
        """Execute one provider batch without reordering observable effects.

        Contiguous calls whose resolved tools explicitly opt into
        ``parallel_safe`` may overlap.  Every other call is a singleton ordering
        barrier, so writers, shell commands, MCP calls without trustworthy
        annotations, and unknown tools retain provider order.
        """
        baseline_error: BaseException | None = None
        try:
            baseline = (
                self._round_interrupt_epoch()
                if interrupt_baseline is None
                else interrupt_baseline
            )
        except BaseException as error:
            baseline = 0
            baseline_error = error
        results = [""] * len(tool_calls)
        resolved: list[tuple[object, BaseException | None, bool]] = []
        for tool_call in tool_calls:
            if baseline_error is not None:
                resolved.append((None, baseline_error, False))
                continue
            try:
                tool = self.agent.get_tool(tool_call.name)
                parallel_safe = bool(
                    tool is not None and getattr(tool, "parallel_safe", False)
                )
            except BaseException as error:
                resolved.append((None, error, False))
            else:
                resolved.append((tool, None, parallel_safe))

        def execute_submitted(
            index: int,
            scheduler_error: BaseException | None = None,
        ) -> str:
            tool, resolution_error, _ = resolved[index]
            return self.execute(
                tool_calls[index],
                interrupt_baseline=(None if scheduler_error is not None else baseline),
                _resolved_tool=tool,
                _resolution_error=(
                    scheduler_error if scheduler_error is not None else resolution_error
                ),
            )

        def publish_scheduler_failure(
            index: int,
            error: BaseException,
            phase: str,
            *,
            effect_started: bool,
        ) -> str:
            failures = _PostEffectFailureCollector()
            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            outcome = (
                self._execution_failure_outcome(phase, error)
                if effect_started
                else self._pre_effect_failure_outcome(
                    phase,
                    error,
                    cancelled=isinstance(error, KeyboardInterrupt),
                )
            )
            if failures:
                self._emit_post_effect_diagnostic(
                    tool_calls[index], failures.snapshot()[0], failures
                )
            return self._publish_post_effect_outcome(
                tool_calls[index],
                tool_calls[index].name,
                outcome,
                failures,
            ).model_text

        def execute_attempt(
            index: int,
            attempt: concurrent.futures.Future[str],
        ) -> str:
            if not attempt.set_running_or_notify_cancel():
                return ""
            try:
                result = execute_submitted(index)
            except BaseException as error:
                attempt.set_exception(error)
                raise
            attempt.set_result(result)
            return result

        def settle_failed_attempt(
            index: int,
            attempt: concurrent.futures.Future[str],
            boundary_error: BaseException,
            phase: str,
        ) -> str:
            if attempt.cancel():
                return publish_scheduler_failure(
                    index,
                    boundary_error,
                    phase,
                    effect_started=False,
                )
            self._queue_batch_runtime_failure(
                phase,
                boundary_error,
                tool_count=1,
            )
            try:
                return attempt.result()
            except BaseException as error:
                return publish_scheduler_failure(
                    index,
                    error,
                    phase,
                    effect_started=True,
                )

        index = 0
        while index < len(tool_calls):
            if not resolved[index][2]:
                results[index] = execute_submitted(index)
                index += 1
                continue

            end = index + 1
            while end < len(tool_calls) and resolved[end][2]:
                end += 1
            if end - index == 1:
                results[index] = execute_submitted(index)
            else:
                try:
                    pool = concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(8, end - index)
                    )
                    entered_pool = pool.__enter__()
                except BaseException as error:
                    for offset in range(index, end):
                        results[offset] = publish_scheduler_failure(
                            offset,
                            error,
                            "parallel_pool",
                            effect_started=False,
                        )
                    index = end
                    continue

                futures: dict[
                    int,
                    tuple[
                        concurrent.futures.Future[str],
                        concurrent.futures.Future[str],
                    ],
                ] = {}
                submit_error: BaseException | None = None
                next_offset = index
                for offset in range(index, end):
                    attempt: concurrent.futures.Future[str] = (
                        concurrent.futures.Future()
                    )
                    try:
                        future = entered_pool.submit(
                            execute_attempt,
                            offset,
                            attempt,
                        )
                    except BaseException as error:
                        submit_error = error
                        results[offset] = settle_failed_attempt(
                            offset,
                            attempt,
                            error,
                            "parallel_submit",
                        )
                        next_offset = offset + 1
                        break
                    futures[offset] = (future, attempt)
                    next_offset = offset + 1

                for offset, (future, attempt) in futures.items():
                    try:
                        results[offset] = future.result()
                    except BaseException as error:
                        results[offset] = settle_failed_attempt(
                            offset,
                            attempt,
                            error,
                            "parallel_future",
                        )

                exit_error: BaseException | None = None
                try:
                    pool.__exit__(None, None, None)
                except BaseException as error:
                    exit_error = error

                if submit_error is not None:
                    for offset in range(next_offset, end):
                        results[offset] = publish_scheduler_failure(
                            offset,
                            submit_error,
                            "parallel_submit",
                            effect_started=False,
                        )
                if exit_error is not None:
                    self._queue_batch_runtime_failure(
                        "parallel_scheduler_cleanup",
                        exit_error,
                        tool_count=end - index,
                    )
            index = end
        return results
