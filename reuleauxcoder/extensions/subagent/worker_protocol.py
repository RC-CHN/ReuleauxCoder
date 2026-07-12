"""Versioned JSON-safe protocol for isolated subagent workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import uuid
from typing import Any, Literal


WORKER_PROTOCOL_VERSION = 1
WorkerMessageType = Literal[
    "ready",
    "runtime_event",
    "tool_request",
    "tool_result",
    "directive",
    "checkpoint",
    "terminal",
]


@dataclass(frozen=True, slots=True)
class WorkerToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    job_id: str
    agent_id: str
    session_id: str | None
    session_generation: int
    worker_generation: int
    cancellation_epoch: int
    delegated_prompt: str
    llm_kwargs: dict[str, Any]
    tools: tuple[WorkerToolSpec, ...]
    max_context_tokens: int
    max_rounds: int
    max_tool_calls: int | None
    max_tokens: int | None
    working_directory: str | None = None
    replay_messages: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_round_trip(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerSpec":
        values = _require_object(data, "worker spec")
        tools = values.get("tools")
        messages = values.get("replay_messages", [])
        if not isinstance(tools, list) or not isinstance(messages, list):
            raise TypeError("worker spec tools/replay_messages must be arrays")
        return cls(
            job_id=_required_str(values, "job_id"),
            agent_id=_required_str(values, "agent_id"),
            session_id=_optional_str(values, "session_id"),
            session_generation=_required_int(values, "session_generation"),
            worker_generation=_required_int(values, "worker_generation"),
            cancellation_epoch=_required_int(values, "cancellation_epoch"),
            delegated_prompt=_required_str(values, "delegated_prompt"),
            llm_kwargs=_require_object(values.get("llm_kwargs"), "llm_kwargs"),
            tools=tuple(
                WorkerToolSpec(
                    name=_required_str(item, "name"),
                    description=_required_str(item, "description"),
                    parameters=_require_object(
                        item.get("parameters"), "tool parameters"
                    ),
                )
                for item in (_require_object(value, "tool spec") for value in tools)
            ),
            max_context_tokens=_required_int(values, "max_context_tokens"),
            max_rounds=_required_int(values, "max_rounds"),
            max_tool_calls=_optional_int(values, "max_tool_calls"),
            max_tokens=_optional_int(values, "max_tokens"),
            working_directory=_optional_str(values, "working_directory"),
            replay_messages=tuple(
                _require_object(message, "replay message") for message in messages
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerEnvelope:
    type: WorkerMessageType
    job_id: str
    agent_id: str
    session_generation: int
    worker_generation: int
    cancellation_epoch: int
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: f"sw_{uuid.uuid4().hex[:16]}")
    payload_hash: str = ""
    version: int = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        canonical = _canonical_hash(self.payload)
        if self.payload_hash and self.payload_hash != canonical:
            raise ValueError("worker envelope payload hash mismatch")
        if not self.payload_hash:
            object.__setattr__(self, "payload_hash", canonical)

    def to_dict(self) -> dict[str, Any]:
        return _json_round_trip(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerEnvelope":
        values = _require_object(data, "worker envelope")
        version = _required_int(values, "version")
        if version != WORKER_PROTOCOL_VERSION:
            raise ValueError(f"unsupported worker protocol version: {version}")
        message_type = _required_str(values, "type")
        if message_type not in {
            "ready",
            "runtime_event",
            "tool_request",
            "tool_result",
            "directive",
            "checkpoint",
            "terminal",
        }:
            raise ValueError(f"unsupported worker message type: {message_type}")
        return cls(
            type=message_type,  # type: ignore[arg-type]
            job_id=_required_str(values, "job_id"),
            agent_id=_required_str(values, "agent_id"),
            session_generation=_required_int(values, "session_generation"),
            worker_generation=_required_int(values, "worker_generation"),
            cancellation_epoch=_required_int(values, "cancellation_epoch"),
            sequence=_required_int(values, "sequence"),
            payload=_require_object(values.get("payload"), "payload"),
            message_id=_required_str(values, "message_id"),
            payload_hash=_required_str(values, "payload_hash"),
            version=version,
        )


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_round_trip(value):
    return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return dict(value)


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


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(f"{key} must be an integer or null")
    return value
