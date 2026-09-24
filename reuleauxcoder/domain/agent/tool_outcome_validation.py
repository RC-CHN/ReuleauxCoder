"""Pure tool-result validation, bounded projections and failure aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
import math
from threading import Lock
from types import MappingProxyType
from typing import cast

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
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    HookDiagnostic,
    HookKind,
    HookPoint,
)

_POST_EFFECT_FAILURE_LIMIT = 8
_POST_EFFECT_FAILURE_COUNT_LIMIT = 1_000_000

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


class InvalidAfterToolPrimaryOutcomeTransition(RuntimeError):
    pass


class InvalidToolResultProjection(TypeError):
    pass


class InvalidToolOutcomeProtocol(TypeError):
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
    "images",
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


class InvalidContextContributionResult(RuntimeError):
    pass
