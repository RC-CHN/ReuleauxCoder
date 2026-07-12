"""Versioned JSON-safe codec for the runtime event boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

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
from reuleauxcoder.domain.runtime import events
from reuleauxcoder.domain.runtime.events import RuntimeEvent, RuntimePayload


RUNTIME_EVENT_CODEC_VERSION = 1

_PAYLOAD_TYPES = {
    payload_type.__name__: payload_type
    for payload_type in (
        events.TurnStarted,
        events.TurnFinished,
        events.AssistantContentDelta,
        events.ReasoningDelta,
        events.ChatStarted,
        events.ChatCompleted,
        events.StreamChunk,
        events.ToolCallStarted,
        events.ToolOutputDelta,
        events.ToolCallFinished,
        events.SubagentJobChanged,
        events.DiagnosticsPublished,
        events.DiagnosticsCleared,
        events.ApprovalRequested,
        events.ApprovalResolved,
        events.ErrorOccurred,
        events.NotificationRaised,
        events.SessionChanged,
        events.RuntimeStateChanged,
        events.PlanUpdated,
        events.ProgressReported,
        events.ViewRequested,
        events.ViewRefreshed,
    )
}


def runtime_event_to_dict(event: RuntimeEvent) -> dict[str, Any]:
    """Encode one event without losing its concrete payload type."""
    return {
        "version": RUNTIME_EVENT_CODEC_VERSION,
        "event_id": event.event_id,
        "timestamp": event.timestamp,
        "agent_id": event.agent_id,
        "session_generation": event.session_generation,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "correlation_id": event.correlation_id,
        "payload": {
            "type": type(event.payload).__name__,
            "data": _encode_dataclass(event.payload),
        },
    }


def runtime_event_from_dict(data: dict[str, Any]) -> RuntimeEvent:
    """Decode one event, rejecting unknown versions and payload tags."""
    if data.get("version") != RUNTIME_EVENT_CODEC_VERSION:
        raise ValueError(f"Unsupported runtime event version: {data.get('version')!r}")
    payload_envelope = data.get("payload")
    if not isinstance(payload_envelope, dict):
        raise TypeError("runtime event payload must be an object")
    payload_type_name = payload_envelope.get("type")
    payload_type = _PAYLOAD_TYPES.get(payload_type_name)
    if payload_type is None:
        raise ValueError(f"Unknown runtime payload type: {payload_type_name!r}")
    payload_data = payload_envelope.get("data")
    if not isinstance(payload_data, dict):
        raise TypeError("runtime payload data must be an object")
    payload = _decode_payload(payload_type_name, payload_type, payload_data)
    return RuntimeEvent(
        payload=payload,
        event_id=_required_str(data, "event_id"),
        timestamp=_required_number(data, "timestamp"),
        agent_id=_optional_str(data, "agent_id"),
        session_generation=_optional_int(data, "session_generation"),
        session_id=_optional_str(data, "session_id"),
        turn_id=_optional_str(data, "turn_id"),
        correlation_id=_optional_str(data, "correlation_id"),
    )


def _encode_dataclass(value: object) -> dict[str, Any]:
    if not is_dataclass(value):
        raise TypeError(f"Expected dataclass, got {type(value).__name__}")
    return {
        item.name: _encode_value(getattr(value, item.name))
        for item in fields(value)
        if item.name != "kind"
    }


def _encode_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _encode_dataclass(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("runtime event mappings require string keys")
        return {key: _encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    raise TypeError(f"Runtime event value is not JSON-safe: {type(value).__name__}")


def _decode_payload(
    type_name: str, payload_type, data: dict[str, Any]
) -> RuntimePayload:
    values = dict(data)
    if type_name == "ToolCallFinished":
        values["outcome"] = _decode_tool_outcome(_required_dict(values, "outcome"))
    elif type_name == "DiagnosticsPublished":
        raw_diagnostics = values.get("diagnostics")
        if not isinstance(raw_diagnostics, list):
            raise TypeError("diagnostics must be an array")
        values["diagnostics"] = tuple(
            events.RuntimeDiagnostic(**_require_string_dict(item, "diagnostic"))
            for item in raw_diagnostics
        )
    elif type_name == "PlanUpdated":
        raw_items = values.get("items")
        if not isinstance(raw_items, list):
            raise TypeError("plan items must be an array")
        values["items"] = tuple(
            _require_string_dict(item, "plan item") for item in raw_items
        )
    try:
        return payload_type(**values)
    except TypeError as error:
        raise TypeError(f"Invalid {type_name} payload: {error}") from error


def _decode_tool_outcome(data: dict[str, Any]) -> ToolOutcome:
    values = dict(data)
    try:
        values["status"] = ToolOutcomeStatus(values["status"])
        if values.get("error_kind") is not None:
            values["error_kind"] = ToolErrorKind(values["error_kind"])
        if values.get("diff") is not None:
            values["diff"] = ToolDiff(**_required_dict(values, "diff"))
        values["diagnostics"] = tuple(
            ToolDiagnostic(**_require_string_dict(item, "tool diagnostic"))
            for item in values.get("diagnostics", [])
        )
        if values.get("truncation") is not None:
            values["truncation"] = ToolTruncation(
                **_required_dict(values, "truncation")
            )
        if values.get("archive_reference") is not None:
            values["archive_reference"] = ToolArchiveReference(
                **_required_dict(values, "archive_reference")
            )
        if values.get("retention_hint") is not None:
            retention = _required_dict(values, "retention_hint")
            values["retention_hint"] = ToolRetentionHint(
                strategy=ToolRetentionStrategy(retention["strategy"]),
                anchor_line=retention.get("anchor_line"),
            )
        return ToolOutcome(**values)
    except (KeyError, TypeError, ValueError) as error:
        raise TypeError(f"Invalid ToolOutcome payload: {error}") from error


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    return _require_string_dict(data.get(key), key)


def _require_string_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(f"{key} must be an integer or null")
    return value


def _required_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{key} must be a number")
    return float(value)
